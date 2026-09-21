# syntax=docker/dockerfile:1.7

FROM ubuntu:26.04

ARG DEBIAN_FRONTEND=noninteractive
ARG ROCM_INSTALLER=rocm-installer-10.0.0-4.run
# Override ROCM_GFX_TARGETS to reduce build time and image size for a specific
# GPU, for example: --build-arg ROCM_GFX_TARGETS=gfx1100
ARG ROCM_GFX_TARGETS="gfx1201"
# A branch can move upstream, but Docker may reuse the cached clone layer.
# Rebuild without cache to refresh a floating ref; a commit pins source content.
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

# Install the container-side ROCm SDK only. Host GPU drivers/devices are supplied
# by the prepared host and AMD Container Toolkit, not by this image.
RUN cd /tmp \
    && curl -fsSLo "${ROCM_INSTALLER}" \
        "https://repo.radeon.com/rocm/installer/rocm-runfile-installer/rocm-rel-10.0/${ROCM_INSTALLER}" \
    && bash "${ROCM_INSTALLER}" \
        deps=install \
        target=/opt \
        compo=core-sdk \
        gfx="${ROCM_GFX_TARGETS}" \
        rocm \
        assumeyes \
    && rm -rf "/tmp/${ROCM_INSTALLER}" /tmp/rocm-installer \
    && /opt/rocm/core-10.0/bin/hipconfig --version

ENV ROCM_PATH="/opt/rocm/core-10.0" \
    PATH="/opt/rocm/core-10.0/bin:${PATH}" \
    LD_LIBRARY_PATH="/opt/rocm/core-10.0/lib"

WORKDIR /opt

# The runfile accepts comma-separated GPU targets; CMake expects semicolons.
# Disable host-native CPU tuning so the image is not tied to the build CPU.
# Build the unified llama application as well as llama-server for model prefetch.
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

ENV PATH="/opt/llama.cpp/build/bin:${PATH}" \
    LD_LIBRARY_PATH="/opt/llama.cpp/build/bin:/opt/rocm/core-10.0/lib" \
    HIP_VISIBLE_DEVICES=0

WORKDIR /models
VOLUME ["/models"]

EXPOSE 8000

ENTRYPOINT ["llama-server"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
