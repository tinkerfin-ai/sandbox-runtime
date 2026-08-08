include versions.env

IMAGE ?= sandbox-runtime:dev
UV_CACHE_DIR ?= .cache/uv
HOST_ARCH := $(shell uname -m)

ifeq ($(HOST_ARCH),arm64)
PLATFORM ?= linux/arm64
else ifeq ($(HOST_ARCH),aarch64)
PLATFORM ?= linux/arm64
else
PLATFORM ?= linux/amd64
endif

BUILD_ARGS = \
	--build-arg PYTHON_IMAGE=$(PYTHON_IMAGE) \
	--build-arg RUNTIME_VERSION=$(RUNTIME_VERSION) \
	--build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
	--build-arg JAVA_VERSION=$(JAVA_VERSION) \
	--build-arg NODE_VERSION=$(NODE_VERSION) \
	--build-arg GO_VERSION=$(GO_VERSION) \
	--build-arg MAVEN_VERSION=$(MAVEN_VERSION) \
	--build-arg NODE_SHA256_AMD64=$(NODE_SHA256_AMD64) \
	--build-arg NODE_SHA256_ARM64=$(NODE_SHA256_ARM64) \
	--build-arg GO_SHA256_AMD64=$(GO_SHA256_AMD64) \
	--build-arg GO_SHA256_ARM64=$(GO_SHA256_ARM64)

.PHONY: build entrypoint-test lock smoke static-test verify

build:
	docker buildx build --load --platform "$(PLATFORM)" --tag "$(IMAGE)" $(BUILD_ARGS) .

lock:
	UV_CACHE_DIR="$(UV_CACHE_DIR)" uv pip compile requirements.in \
		--output-file requirements.lock \
		--python-version 3.11 \
		--python-platform linux \
		--generate-hashes \
		--only-binary :all: \
		--default-index https://pypi.org/simple \
		--custom-compile-command 'make lock'

entrypoint-test:
	./tests/entrypoint-env.sh

static-test:
	./tests/static-contract.sh

smoke: build
	./tests/runtime-smoke.sh "$(IMAGE)"

verify: entrypoint-test static-test
