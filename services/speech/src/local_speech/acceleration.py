"""加速 token 解码与等价声学掩码，保留 CosyVoice 的声学步数与训练块长。"""

import os
from collections.abc import Callable
from threading import Lock
from typing import Any


def enable_gpu_mask() -> Callable[[], None]:
    """静态 DiT 掩码以等价布尔计算替代逐步 .item() 同步，不改权重、精度、步数或音频块。"""
    import torch
    from cosyvoice.flow.DiT import dit
    from cosyvoice.utils.mask import subsequent_chunk_mask

    original = dit.add_optional_chunk_mask

    def gpu_mask(
        xs,
        masks,
        use_dynamic_chunk,
        use_dynamic_left_chunk,
        decoding_chunk_size,
        static_chunk_size,
        num_decoding_left_chunks,
        enable_full_context=True,
    ):
        """仅固定推理路径走 GPU；全零行与填充逐值保留，动态训练选项仍调用原库。"""
        if use_dynamic_chunk or use_dynamic_left_chunk or decoding_chunk_size != 0:
            return original(
                xs,
                masks,
                use_dynamic_chunk,
                use_dynamic_left_chunk,
                decoding_chunk_size,
                static_chunk_size,
                num_decoding_left_chunks,
                enable_full_context,
            )
        chunks = masks
        if static_chunk_size > 0:
            chunks = masks & subsequent_chunk_mask(
                xs.size(1), static_chunk_size, num_decoding_left_chunks, xs.device
            ).unsqueeze(0)
        if chunks.dtype != torch.bool:
            raise TypeError("声学掩码必须为布尔张量")
        return chunks | ~chunks.any(dim=-1, keepdim=True)

    def restore() -> None:
        """仅还原自己的进程内适配器，关闭或初始化失败均不得覆盖其他所有者的新实现。"""
        if dit.add_optional_chunk_mask is gpu_mask:
            dit.add_optional_chunk_mask = original

    dit.add_optional_chunk_mask = gpu_mask
    return restore


def enable_flow_graph(model: Any) -> Callable[[], None]:
    """图只包裹声学解码器；初始化失败或退出必须撤回原模型和进程内掩码。"""
    from local_speech.flow_graph import FlowGraph

    original = model.flow.decoder
    original_noise = original.rand_noise
    restore_mask = enable_gpu_mask()
    try:
        graph = FlowGraph(original)
        model.flow.decoder = graph
    except BaseException:
        original.rand_noise = original_noise
        restore_mask()
        raise

    def restore() -> None:
        """只有本次包装仍在使用时才还原，显存释放由会话模型的关闭边界负责。"""
        if model.flow.decoder is graph:
            model.flow.decoder = original
        original.rand_noise = original_noise
        restore_mask()

    return restore


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
        # 同卡还运行声学模型，不按剩余显存自动扩张 KV 缓存。
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
