include versions.env
include opensandbox-server/versions.env
include opensandbox-execd/versions.env

IMAGE ?= sandbox-runtime:dev
SERVER_IMAGE ?= opensandbox-server:dev
EXECD_IMAGE ?= opensandbox-execd:dev
EXECD_RUNTIME_IMAGE ?= ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.2
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
	--build-arg NPM_VERSION=$(NPM_VERSION) \
	--build-arg GO_VERSION=$(GO_VERSION) \
	--build-arg MAVEN_VERSION=$(MAVEN_VERSION) \
	--build-arg SETUPTOOLS_VERSION=$(SETUPTOOLS_VERSION) \
	--build-arg NPM_BRACE_EXPANSION_VERSION=$(NPM_BRACE_EXPANSION_VERSION) \
	--build-arg NPM_UNDICI_VERSION=$(NPM_UNDICI_VERSION) \
	--build-arg NPM_IP_ADDRESS_VERSION=$(NPM_IP_ADDRESS_VERSION) \
	--build-arg NPM_TAR_VERSION=$(NPM_TAR_VERSION) \
	--build-arg NPM_TAR_SHA512=$(NPM_TAR_SHA512) \
	--build-arg NODE_SHA256_AMD64=$(NODE_SHA256_AMD64) \
	--build-arg NODE_SHA256_ARM64=$(NODE_SHA256_ARM64) \
	--build-arg NPM_SHA512=$(NPM_SHA512) \
	--build-arg NPM_BRACE_EXPANSION_SHA512=$(NPM_BRACE_EXPANSION_SHA512) \
	--build-arg NPM_UNDICI_SHA512=$(NPM_UNDICI_SHA512) \
	--build-arg NPM_IP_ADDRESS_SHA512=$(NPM_IP_ADDRESS_SHA512) \
	--build-arg SETUPTOOLS_SHA256=$(SETUPTOOLS_SHA256) \
	--build-arg GO_SHA256_AMD64=$(GO_SHA256_AMD64) \
	--build-arg GO_SHA256_ARM64=$(GO_SHA256_ARM64)

.PHONY: build entrypoint-test execd-build execd-test lock runtime-signals server-build server-test smoke static-test verify workspace-test

build:
	docker buildx build --load --platform "$(PLATFORM)" --tag "$(IMAGE)" $(BUILD_ARGS) .

server-build:
	docker buildx build --load --platform "$(PLATFORM)" --file opensandbox-server/Dockerfile \
		--build-arg SERVER_VERSION="$(SERVER_VERSION)" --tag "$(SERVER_IMAGE)" .

server-test:
	bash tests/server-image.sh "$(SERVER_IMAGE)"
	python3 tests/server-image-lifecycle.py "$(SERVER_IMAGE)"

execd-build:
	docker buildx build --load --platform "$(PLATFORM)" --file opensandbox-execd/Dockerfile \
		--build-arg EXECD_VERSION="$(EXECD_VERSION)" --tag "$(EXECD_IMAGE)" .

execd-test:
	EXECD_RUNTIME_IMAGE="$(EXECD_RUNTIME_IMAGE)" bash opensandbox-execd/tests/image.sh "$(EXECD_IMAGE)"
	EXECD_RUNTIME_IMAGE="$(EXECD_RUNTIME_IMAGE)" python3 opensandbox-execd/tests/signals.py "$(EXECD_IMAGE)"

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
	python3 tests/release-contract.py

smoke: build
	./tests/runtime-smoke.sh "$(IMAGE)"
	$(MAKE) workspace-test IMAGE="$(IMAGE)"
	$(MAKE) runtime-signals IMAGE="$(IMAGE)"

workspace-test:
	bash tests/workspace-runtime.sh "$(IMAGE)"

runtime-signals:
	python3 opensandbox-execd/tests/signals.py --runtime-image "$(IMAGE)"

verify: entrypoint-test static-test
