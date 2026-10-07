"""只加速语音 token 解码，保留 CosyVoice 的声学步数与训练块长。"""

import os
from threading import Lock
from typing import Any


def enable_vllm(model: Any, model_dir: str) -> None:
    """单条合成共享显卡，显式限制 KV 显存并禁用遥测；引擎失败必须向上传播。"""
    # 子进程也继承这两个开关，不能在初始化后才禁止引擎的使用统计。
    os.environ["VLLM_NO_USAGE_STATS"] = "1"
    os.environ["VLLM_DO_NOT_TRACK"] = "1"
    os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"

    import torch
    from cosyvoice.utils.file_utils import export_cosyvoice2_vllm
    from vllm import EngineArgs, LLMEngine, ModelRegistry

    ModelRegistry.register_model(
        "CosyVoice2ForCausalLM", "cosyvoice.vllm.cosyvoice2:CosyVoice2ForCausalLM"
    )
    export_dir = os.path.join(model_dir, "vllm")
    export_cosyvoice2_vllm(model.llm, export_dir, model.device)
    args = EngineArgs(
        model=export_dir,
        skip_tokenizer_init=True,
        enable_prompt_embeds=True,
        # 同卡还运行声学模型与对话 Qwen，不按剩余显存自动扩张 KV 缓存。
        gpu_memory_utilization=0.20,
        kv_cache_memory_bytes=512 * 2**20,
        max_model_len=model.llm.llm.model.config.max_position_embeddings,
        max_num_seqs=1,
        max_num_batched_tokens=2048,
        # 当前 V1 的 prompt embeddings 不支持前缀缓存，明确关闭避免假加速。
        enable_prefix_caching=False,
        disable_log_stats=True,
    )
    model.llm.vllm = LLMEngine.from_engine_args(args)
    model.llm.lock = Lock()
    # 保留文本与语音嵌入，Transformer 层已由 vLLM 持有，及时释放原始副本。
    del model.llm.llm.model.model.layers
    torch.cuda.empty_cache()
