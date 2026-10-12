"""Enable private-network sessions in the pinned server during image construction.

Only the trusted execd parent receives NET_ADMIN. Session callers remain
responsible for choosing a non-root session user and restricting its filesystem.
The source digest identifies OpenSandbox commit
c39b814f36ded4c61d5ac6f9332ee4dfbab86c00. Unknown source fails the build.
"""

from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

SOURCE = Path("/app/opensandbox_server/services/docker/docker_service.py")
SOURCE_SHA256 = "aa3222ebbdd52e0c6556e461855451cd939369305909b86101c5189cb0101e04"
NETWORK_SOURCE = Path("/app/opensandbox_server/services/docker/networking.py")
NETWORK_SOURCE_SHA256 = (
    "52d0deea464460224557a799d7b1f391a0b2da235309c611d841d0060e4095fe"
)
CONTAINER_SOURCE = Path("/app/opensandbox_server/services/docker/container_ops.py")
CONTAINER_SOURCE_SHA256 = (
    "e658519053f51526368c7bf9a4ab0d91b65d1a7100529524acf0214d7741086c"
)


PROXY_SOURCE = Path("/app/opensandbox_server/api/proxy.py")
PROXY_SOURCE_SHA256 = "954bdb12221517fadb818b11dfea9419325c7099df604d6b0027431688485254"
METADATA_SOURCE = Path("/app/opensandbox_server/services/docker/metadata.py")
METADATA_SOURCE_SHA256 = (
    "d6013d994a031121ff1c8587994dccaeb2a64a0a15377300bf56476622e27ce8"
)


def patched_source(
    path: Path, expected_digest: str, replacements: dict[str, str]
) -> bytes:
    """Return a checked correction for the exact upstream source."""
    source = path.read_bytes()
    if sha256(source).hexdigest() != expected_digest:
        raise RuntimeError(
            "The OpenSandbox server source does not match the pinned commit"
        )
    for original, replacement in replacements.items():
        if source.count(original.encode()) != 1:
            raise RuntimeError(
                f"The OpenSandbox server patch target is ambiguous: {path.name}: {original!r}"
            )
        source = source.replace(original.encode(), replacement.encode(), 1)
    compile(source, str(path), "exec")
    return source


def main() -> None:
    """Apply the fixed source correction without changing the server entrypoint."""
    if version("opensandbox-server") != "0.2.3":
        raise RuntimeError("The server patch requires OpenSandbox Server 0.2.3")
    replacements = {
        "from opensandbox_server.services.docker.ossfs_mixin import OSSFSMixin": "from opensandbox_server.services.docker.ossfs_mixin import OSSFSMixin\n"
        "from opensandbox_server.services.docker.persistent import PersistentSandboxLifecycle",
        "        self._restore_existing_sandboxes()": "        self._persistent_lifecycle = PersistentSandboxLifecycle(self.docker_client, self._metadata_store._root)\n"
        "        self._restore_existing_sandboxes()",
        "            mount_keys = _parse_and_accumulate_mount_refs(labels)": "            self._persistent_lifecycle.restore_pause(sandbox_id, container)\n"
        "            mount_keys = _parse_and_accumulate_mount_refs(labels)",
        "        expires_at = self._get_tracked_expiration(resolved_id, labels)\n\n        status_info = SandboxStatus(": "        expires_at = self._get_tracked_expiration(resolved_id, labels)\n\n"
        "        if self._persistent_lifecycle.is_paused(resolved_id):\n"
        "            state = 'Paused'\n"
        "            reason = 'EXPLICIT_PAUSE'\n"
        "            message = 'Sandbox requires explicit resumption.'\n\n"
        "        status_info = SandboxStatus(",
        "                    extra_env=egress_env or None,": "                    extra_env=egress_env or None,\n"
        "                    persistent=expires_at is None,",
        "                container.pause()": "                if self._persistent_lifecycle.retained(container):\n"
        "                    self._persistent_lifecycle.pause(sandbox_id, container)\n"
        "                else:\n"
        "                    container.pause()",
        '        if not state.get("Paused", False):': '        if not state.get("Paused", False) and not self._persistent_lifecycle.is_paused(sandbox_id):',
        "                container.unpause()": "                if self._persistent_lifecycle.retained(container):\n"
        "                    self._persistent_lifecycle.resume(sandbox_id, container)\n"
        "                else:\n"
        "                    container.unpause()",
        "        self._validate_network_exists()": "        self._validate_network_exists(\n"
        '            require_private_namespace=(request.extensions or {}).get(BOOTSTRAP_EXECD_ISOLATION_KEY) == "enable",\n'
        "        )",
        'cap_add.add("SYS_ADMIN")': 'cap_add.update({"SYS_ADMIN", "NET_ADMIN"})',
        "# Drop NET_ADMIN for the main container; only the sidecar should keep it": "# Drop NET_ADMIN unless the isolation extension below needs it for bwrap.",
        "# Inject CAP_SYS_ADMIN + unconfined AppArmor when bwrap isolation is requested.\n"
        "            # bwrap needs pivot_root/mount which require CAP_SYS_ADMIN and are blocked\n"
        "            # by Docker's default AppArmor profile.": "# TinkerFin modification: execd needs SYS_ADMIN for mounts and NET_ADMIN\n"
        "            # for loopback setup inside a private network namespace.\n"
        "            # Isolated commands must use a non-root session user.",
        "granting CAP_SYS_ADMIN + apparmor/seccomp=unconfined": "granting CAP_SYS_ADMIN + CAP_NET_ADMIN + apparmor/seccomp=unconfined",
    }
    network_replacements = {
        "        extra_env: Optional[Dict[str, Optional[str]]] = None,\n    ):": "        extra_env: Optional[Dict[str, Optional[str]]] = None,\n"
        "        persistent: bool = False,\n    ):",
        '            "port_bindings": normalize_port_bindings(sidecar_port_bindings),\n        }': '            "port_bindings": normalize_port_bindings(sidecar_port_bindings),\n        }\n'
        '        if persistent:\n            base_sidecar_host_config_kwargs["restart_policy"] = {"Name": "unless-stopped"}',
        "    def _validate_network_exists(self) -> None:\n"
        '        """Verify the configured user-defined Docker network exists before creating a sandbox."""': "    def _validate_network_exists(self, *, require_private_namespace: bool = False) -> None:\n"
        '        """Validate the network and reject shared namespaces for isolated requests."""\n'
        "        # TinkerFin modification: grant parent capabilities only in its own network namespace.\n"
        "        if require_private_namespace and (\n"
        "            self.network_mode == HOST_NETWORK_MODE\n"
        '            or self.network_mode.startswith("container:")\n'
        "        ):\n"
        "            raise HTTPException(\n"
        "                status_code=status.HTTP_400_BAD_REQUEST,\n"
        "                detail={\n"
        '                    "code": SandboxErrorCodes.INVALID_PARAMETER,\n'
        "                    \"message\": \"Isolated sessions require docker.network_mode='bridge', 'none', or a user-defined network with its own network namespace.\",\n"
        "                },\n"
        "            )",
        "            self.docker_client.networks.get(self.network_mode)": "            network = self.docker_client.networks.get(self.network_mode)\n"
        '            if require_private_namespace and network.attrs.get("Driver") == "host":\n'
        "                raise HTTPException(\n"
        "                    status_code=status.HTTP_400_BAD_REQUEST,\n"
        "                    detail={\n"
        '                        "code": SandboxErrorCodes.INVALID_PARAMETER,\n'
        '                        "message": "Isolated sessions cannot use a Docker network backed by the host driver.",\n'
        "                    },\n"
        "                )",
    }
    network_replacements.update(
        {
            "            self._attach_egress_auth_headers(endpoint, labels, port)\n            return endpoint\n\n        # non-host": "            self._attach_egress_auth_headers(endpoint, labels, port)\n            if port == 44772:\n                self._attach_execd_access_headers(endpoint, labels)\n            return endpoint\n\n        # non-host",
            '        endpoint = Endpoint(endpoint=f"{public_host}:{execd_host_port}/proxy/{port}")': '        endpoint = Endpoint(endpoint=f"{public_host}:{execd_host_port}/proxy/{port}")\n        self._attach_execd_access_headers(endpoint, labels)',
            "    def _attach_egress_auth_headers(\n": '''    def _attach_execd_access_headers(self, endpoint: Endpoint, labels: dict[str, str]) -> None:
        """Authenticate access to the privileged isolation parent, not user ports."""
        if labels.get("opensandbox.io/execd-isolation") != "enable":
            return
        token = labels.get("opensandbox.io/execd-access-token", "")
        if len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": SandboxErrorCodes.NETWORK_MODE_ENDPOINT_UNAVAILABLE,
                    "message": "Isolated execd authentication is unavailable.",
                },
            )
        endpoint.headers = merge_endpoint_headers(endpoint.headers, {"X-EXECD-ACCESS-TOKEN": token})

    def _attach_egress_auth_headers(
''',
            '        if self.network_mode == HOST_NETWORK_MODE:\n            return Endpoint(endpoint=f"127.0.0.1:{port}")\n\n        ip_address = self._extract_bridge_ip(container)\n        return Endpoint(endpoint=f"{ip_address}:{port}")': """        if self.network_mode == HOST_NETWORK_MODE:
            endpoint = Endpoint(endpoint=f"127.0.0.1:{port}")
        else:
            ip_address = self._extract_bridge_ip(container)
            endpoint = Endpoint(endpoint=f"{ip_address}:{port}")
        if port == 44772:
            labels = container.attrs.get("Config", {}).get("Labels") or {}
            self._attach_execd_access_headers(endpoint, labels)
        return endpoint""",
        }
    )
    container_replacements = {
        "        host_config = self.docker_client.api.create_host_config(**host_config_kwargs)": '        if labels.get("opensandbox.io/manual-cleanup") == "true":\n'
        '            host_config_kwargs = {**host_config_kwargs, "restart_policy": {"Name": "unless-stopped"}}\n'
        "        host_config = self.docker_client.api.create_host_config(**host_config_kwargs)",
        "import logging\n": "import logging\nimport secrets\n",
        "        env_dict = {**(self.app_config.docker.sandbox_env or {}), **(request.env or {})}": """        env_dict = {**(self.app_config.docker.sandbox_env or {}), **(request.env or {})}
        if (request.extensions or {}).get("bootstrap.execd.isolation") == "enable":
            token = secrets.token_hex(32)
            labels["opensandbox.io/execd-isolation"] = "enable"
            labels["opensandbox.io/execd-access-token"] = token
            env_dict["EXECD_ACCESS_TOKEN"] = token""",
    }
    proxy_replacements = {
        'detail=f"Could not connect to the backend sandbox {endpoint}: {e}",': 'detail="Could not connect to the backend sandbox",',
        'status_code=500, detail=f"An internal error occurred in the proxy: {e}"': 'status_code=500, detail="An internal error occurred in the proxy"',
        "def _filter_proxy_headers(\n": '''def _uses_execd_proxy(endpoint: Endpoint, port: int) -> bool:
    """Identify the actual Docker hop, including sidecar host-mapped endpoints."""
    return port == 44772 or urlsplit("http://" + endpoint.endpoint).path == f"/proxy/{port}"


def _filter_proxy_headers(
''',
        "    extra_excluded: Optional[set[str]] = None,\n": "    execd_target: bool = False,\n    extra_excluded: Optional[set[str]] = None,\n",
        "    if extra_excluded:\n        excluded.update(extra_excluded)": '    if not execd_target:\n        excluded.add("x-execd-access-token")\n    if extra_excluded:\n        excluded.update(extra_excluded)',
        "            request.headers,\n            endpoint.headers,\n": "            request.headers,\n            endpoint.headers,\n            execd_target=_uses_execd_proxy(endpoint, port),\n",
        "        dict(websocket.headers),\n        endpoint.headers,\n": "        dict(websocket.headers),\n        endpoint.headers,\n        execd_target=_uses_execd_proxy(endpoint, port),\n",
    }
    source = patched_source(SOURCE, SOURCE_SHA256, replacements)
    metadata_source = patched_source(
        METADATA_SOURCE,
        METADATA_SOURCE_SHA256,
        {
            "import json\n": "import json\nfrom hashlib import sha256\n",
            "        for path in (self._sandbox_path(sandbox_id), self._expiration_path(sandbox_id)):": "        for path in (self._sandbox_path(sandbox_id), self._expiration_path(sandbox_id),\n"
            '                     self._root / "_lifecycle" / f"{sha256(sandbox_id.encode()).hexdigest()}.json"):',
        },
    )
    network_source = patched_source(
        NETWORK_SOURCE, NETWORK_SOURCE_SHA256, network_replacements
    )
    container_source = patched_source(
        CONTAINER_SOURCE, CONTAINER_SOURCE_SHA256, container_replacements
    )
    proxy_source = patched_source(PROXY_SOURCE, PROXY_SOURCE_SHA256, proxy_replacements)
    SOURCE.write_bytes(source)
    NETWORK_SOURCE.write_bytes(network_source)
    CONTAINER_SOURCE.write_bytes(container_source)
    PROXY_SOURCE.write_bytes(proxy_source)
    METADATA_SOURCE.write_bytes(metadata_source)


if __name__ == "__main__":
    main()
