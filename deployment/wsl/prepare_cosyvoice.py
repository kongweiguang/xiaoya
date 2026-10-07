"""为固定上游源码补充本地规则入口，避免构造前端时隐式联网下载。"""

import sys
from pathlib import Path


def main() -> None:
    """只补充前端配置入口；上下文漂移明确失败，重复部署保留同一补丁。"""
    root = Path(sys.argv[1])
    target = root / "cosyvoice/cli/frontend.py"
    text = target.read_text(encoding="utf-8")
    if "SPEECH_TTS_NORMALIZER_PATH" in text:
        return
    anchor = "        # NOTE compatible when no text frontend tool is avaliable\n"
    if text.count(anchor) != 1:
        raise RuntimeError("CosyVoice 前端源码与固定版本不一致")
    local_frontend = """        # 直接读取已校验的规则，不能让上游默认下载失败后悄悄关闭数字规范化。
        if normalizer_root := os.environ.get('SPEECH_TTS_NORMALIZER_PATH'):
            from wetext import Normalizer
            self.zh_tn_model = Normalizer(
                tagger_path=os.path.join(normalizer_root, 'zh/tn/tagger.fst'),
                verbalizer_path=os.path.join(normalizer_root, 'zh/tn/verbalizer.fst'),
                lang='zh', remove_erhua=False)
            self.en_tn_model = Normalizer(
                tagger_path=os.path.join(normalizer_root, 'en/tn/tagger.fst'),
                verbalizer_path=os.path.join(normalizer_root, 'en/tn/verbalizer.fst'),
                lang='en')
            self.text_frontend = 'wetext'
            return
"""
    signature = "                 allowed_special: str = 'all'):\n"
    if text.count(signature) != 1:
        raise RuntimeError("CosyVoice 前端构造签名与固定版本不一致")
    text = text.replace(
        signature,
        signature
        + '        """本地规则入口避免通话期间联网，同时保留上游分词和音色提取策略。"""\n',
        1,
    )
    target.write_text(text.replace(anchor, local_frontend + anchor, 1), encoding="utf-8")


if __name__ == "__main__":
    main()
