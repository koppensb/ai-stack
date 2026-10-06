FROM rocm/dev-ubuntu-26.04:latest

ARG DEBIAN_FRONTEND=noninteractive

# Override ROCM_GFX_TARGETS to reduce build time and image size for a specific GPU
ARG ROCM_GFX_TARGETS="gfx1201"

# Build branch/tag/commit of llama.cpp
ARG LLAMA_CPP_REF=master

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        bash \
        cmake \
        curl \
        git \
        libcurl4-openssl-dev \
        libgomp1 \
        ninja-build \
        rsync \
    && rm -rf /var/lib/apt/lists/*

# ROCm ist bereits im Base-Image vorhanden
ENV ROCM_PATH=/opt/rocm \
    PATH=/opt/rocm/bin:${PATH} \
    LD_LIBRARY_PATH=/opt/rocm/lib:${LD_LIBRARY_PATH}

WORKDIR /opt

RUN git clone --filter=blob:none https://github.com/ggml-org/llama.cpp.git \
    && cd llama.cpp \
    && git checkout "${LLAMA_CPP_REF}" \
    && AMDGPU_TARGETS="$(printf '%s' "${ROCM_GFX_TARGETS}" | tr ',' ';')" \
    && HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$(hipconfig -R)" \
       cmake -S . -B build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_HIP=ON \
        -DGGML_NATIVE=OFF \
        -DAMDGPU_TARGETS="${AMDGPU_TARGETS}" \
        -DLLAMA_CURL=ON \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_APP=ON \
        -DLLAMA_BUILD_TOOLS=ON \
        -DLLAMA_BUILD_EXAMPLES=ON \
    && cmake --build build --config Release --parallel "$(nproc)" \
    && test -x build/bin/llama \
    && build/bin/llama download --help >/dev/null

ENV PATH=/opt/llama.cpp/build/bin:${PATH} \
    LD_LIBRARY_PATH=/opt/llama.cpp/build/bin:/opt/rocm/lib:${LD_LIBRARY_PATH} \
    HIP_VISIBLE_DEVICES=0

WORKDIR /models
VOLUME ["/models"]

EXPOSE 8000

ENTRYPOINT ["llama-server"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
