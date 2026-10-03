"""Embedding 推理服务：一次推理同时输出【稠密向量】与【稀疏向量】。

为什么需要稀疏向量：纯稠密向量对"型号、文号、金额、条款号"这类精确字符串不敏感，
而财税场景大量依赖精确匹配（例如"财税〔2024〕12 号"）。两种向量配合使用，
由检索层做 RRF 融合（见技术方案第 3.5 节）。

两种后端：
  stub   —— 确定性哈希占位实现。不下载模型、秒级启动，用于环境联调与自动化测试。
            同样的输入永远得到同样的向量，因此可以写确定性测试。
  bge_m3 —— 真实 BAAI/bge-m3，输出 1024 维稠密 + 稀疏词权重。
            首次启动需下载约 2.3GB 模型。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("embedding")

BACKEND = os.getenv("EMBEDDING_BACKEND", "stub").strip().lower()
MODEL_NAME = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
DENSE_DIM = int(os.getenv("DENSE_DIM", "1024"))
SPARSE_INDEX_SPACE = int(os.getenv("SPARSE_INDEX_SPACE", "1048576"))
MAX_LENGTH = int(os.getenv("EMBEDDING_MAX_LENGTH", "8192"))

# 中英文字符 + 数字串：中文按字切分，英文数字按词切分
_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]|[A-Za-z]+|[0-9]+(?:[.\-][0-9]+)*")

app = FastAPI(
    title="Embedding 服务",
    version="0.1.0",
    description="输出稠密 + 稀疏双向量的向量化服务（bge-m3 接口契约）。",
)


class EmbedRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1, description="待向量化的文本列表")


class SparseVector(BaseModel):
    indices: list[int]
    values: list[float]


class EmbedResponse(BaseModel):
    backend: str
    model: str
    dense_dim: int
    count: int
    dense: list[list[float]]
    sparse: list[SparseVector]


# --------------------------------------------------------------------------
# stub 后端：确定性哈希
# --------------------------------------------------------------------------

def _seeded_unit_vector(token: str, dim: int) -> np.ndarray:
    """由字符串确定性地生成一个单位向量。"""

    digest = hashlib.sha256(token.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big", signed=False)
    rng = np.random.default_rng(seed)
    vector = rng.standard_normal(dim)
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        vector[0] = 1.0
        return vector
    return vector / norm


def _tokenize(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


def _stub_embed(texts: list[str]) -> tuple[list[list[float]], list[SparseVector]]:
    dense_vectors: list[list[float]] = []
    sparse_vectors: list[SparseVector] = []

    for text in texts:
        tokens = _tokenize(text)
        if not tokens:
            tokens = ["<empty>"]

        # 稠密：以整段文本的哈希为基底，叠加各 token 的分量，再单位化。
        # 同样的输入 => 同样的输出；包含相近 token 的两段文本 => 向量较接近。
        accumulator = np.zeros(DENSE_DIM, dtype=np.float64)
        for token in tokens:
            accumulator += _seeded_unit_vector(token, DENSE_DIM)
        norm = float(np.linalg.norm(accumulator))
        accumulator = accumulator / norm if norm > 0 else accumulator
        dense_vectors.append([round(float(value), 8) for value in accumulator])

        # 稀疏：token -> 哈希索引，值为出现次数（做 sqrt 压缩避免长文本权重过大）
        weights: dict[int, float] = {}
        for token in tokens:
            index = int.from_bytes(
                hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big"
            ) % SPARSE_INDEX_SPACE
            weights[index] = weights.get(index, 0.0) + 1.0
        items = sorted(weights.items())
        sparse_vectors.append(
            SparseVector(
                indices=[index for index, _ in items],
                values=[round(float(np.sqrt(value)), 8) for _, value in items],
            )
        )

    return dense_vectors, sparse_vectors


# --------------------------------------------------------------------------
# bge_m3 后端：真实模型（惰性加载）
# --------------------------------------------------------------------------

_model: Any = None


def _use_fp16() -> bool:
    """是否用半精度推理。

    只有真有 CUDA 显卡时才开：CPU 上跑 fp16 要么直接报错，
    要么因为反复做类型转换比 fp32 还慢。
    需要人工指定时用环境变量 EMBEDDING_USE_FP16=1 / 0。
    """

    override = os.getenv("EMBEDDING_USE_FP16", "").strip().lower()
    if override in {"1", "true", "yes"}:
        return True
    if override in {"0", "false", "no"}:
        return False
    try:
        import torch  # 延迟导入：stub 模式不会走到这里

        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001 - 判断不了就按 CPU 处理，安全兜底
        return False


def _get_model() -> Any:
    global _model
    if _model is None:
        from FlagEmbedding import BGEM3FlagModel  # 延迟导入：stub 模式无需安装

        use_fp16 = _use_fp16()
        logger.info(
            "正在加载 bge-m3：%s（use_fp16=%s，首次加载约 1 分钟）", MODEL_NAME, use_fp16
        )
        _model = BGEM3FlagModel(MODEL_NAME, use_fp16=use_fp16)
        logger.info("bge-m3 加载完成")
    return _model


def _bge_m3_embed(texts: list[str]) -> tuple[list[list[float]], list[SparseVector]]:
    output = _get_model().encode(
        texts,
        max_length=MAX_LENGTH,
        return_dense=True,
        return_sparse=True,
        return_colbert_vecs=False,
    )

    dense_vectors = [[float(value) for value in row] for row in output["dense_vecs"]]

    sparse_vectors: list[SparseVector] = []
    for lexical in output["lexical_weights"]:
        items = sorted((int(index), float(value)) for index, value in lexical.items())
        sparse_vectors.append(
            SparseVector(
                indices=[index for index, _ in items],
                values=[round(value, 8) for _, value in items],
            )
        )

    return dense_vectors, sparse_vectors


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------

@app.get("/health", summary="健康检查")
def health() -> dict[str, object]:
    device = "cpu"
    try:
        import torch

        if torch.cuda.is_available():
            device = f"cuda:{torch.cuda.get_device_name(0)}"
    except Exception:  # noqa: BLE001 - stub 模式没有 torch，不必报错
        device = "n/a（占位后端）" if BACKEND == "stub" else "cpu"

    return {
        "status": "ok",
        "backend": BACKEND,
        "model": MODEL_NAME,
        "dense_dim": DENSE_DIM,
        "model_loaded": _model is not None,
        "device": device,
    }


@app.post("/embed", response_model=EmbedResponse, summary="文本向量化（稠密 + 稀疏）")
def embed(payload: EmbedRequest) -> EmbedResponse:
    texts = [text if text.strip() else " " for text in payload.texts]

    if BACKEND == "bge_m3":
        dense, sparse = _bge_m3_embed(texts)
    elif BACKEND == "stub":
        dense, sparse = _stub_embed(texts)
    else:
        raise HTTPException(
            status_code=500,
            detail=f"未知的 EMBEDDING_BACKEND={BACKEND!r}，可选：stub | bge_m3",
        )

    if dense and len(dense[0]) != DENSE_DIM:
        raise HTTPException(
            status_code=500,
            detail=f"向量维度不符：期望 {DENSE_DIM}，实际 {len(dense[0])}",
        )

    return EmbedResponse(
        backend=BACKEND,
        model=MODEL_NAME,
        dense_dim=len(dense[0]) if dense else DENSE_DIM,
        count=len(texts),
        dense=dense,
        sparse=sparse,
    )
