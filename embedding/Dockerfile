FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 是否安装重量级依赖（FlagEmbedding + PyTorch）
# 默认 false：镜像小、启动快，使用 stub 后端
# 需要真实 bge-m3 时：docker compose build --build-arg INSTALL_HEAVY=true embedding
ARG INSTALL_HEAVY=false

# PyTorch 的下载源。默认装 CPU 版：
#   · 这台机器上容器用不到显卡（Docker Desktop 默认运行时是 runc，不给容器透传 GPU），
#     而默认 PyPI 的 torch 是 CUDA 版，会连带拖下 2～3GB 的 nvidia-* 依赖，
#     构建要一两个小时，装完还用不上。
# 以后真要给容器用显卡，改成： --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

WORKDIR /app

COPY requirements.txt requirements-heavy.txt ./
RUN if [ "$INSTALL_HEAVY" = "true" ]; then \
        pip install --no-cache-dir --index-url "$TORCH_INDEX_URL" "torch>=2.4,<3" && \
        pip install --no-cache-dir -r requirements-heavy.txt ; \
    else \
        pip install --no-cache-dir -r requirements.txt ; \
    fi

COPY . .

EXPOSE 8001

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
