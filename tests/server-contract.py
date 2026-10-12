"""Exercise provisioning contracts in the installed server without a Docker socket."""

import json
import unittest
from contextlib import ExitStack
from importlib.metadata import version
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from opensandbox_server.api.schema import CreateSandboxRequest
from opensandbox_server.config import (
    AppConfig,
    DockerConfig,
    EgressConfig,
    RuntimeConfig,
)
from opensandbox_server.services.constants import (
    SANDBOX_EGRESS_AUTH_TOKEN_METADATA_KEY,
    SANDBOX_EMBEDDING_PROXY_PORT_LABEL,
    SANDBOX_HTTP_PORT_LABEL,
)
from opensandbox_server.services.docker import DockerSandboxService
from opensandbox_server.services.docker.networking import EGRESS_SIDECAR_LABEL
from opensandbox_server.services.docker.persistent import PersistentSandboxLifecycle

with (
    patch("opensandbox_server.services.factory.create_sandbox_service"),
    patch("opensandbox_server.services.snapshot_service.create_snapshot_service"),
):
    from opensandbox_server.api import proxy


class ProvisioningContract(unittest.IsolatedAsyncioTestCase):
    """Verify that the existing isolation extension changes only its own permissions."""

    def test_isolation_authentication_is_private_and_endpoint_specific(self) -> None:
        docker_client = MagicMock()
        docker_client.containers.list.return_value = []
        config = AppConfig(
            runtime=RuntimeConfig(
                type="docker", execd_image="opensandbox/execd:v1.0.22"
            ),
            docker=DockerConfig(
                network_mode="bridge",
                sandbox_env={"EXECD_ACCESS_TOKEN": "config-token"},
            ),
        )
        request = CreateSandboxRequest.model_validate(
            {
                "image": {"uri": "contract-runtime"},
                "timeout": 120,
                "resourceLimits": {},
                "entrypoint": ["/bin/sh"],
                "env": {"EXECD_ACCESS_TOKEN": "request-token"},
                "extensions": {"bootstrap.execd.isolation": "enable"},
            }
        )
        with patch(
            "opensandbox_server.services.docker.docker_service.docker.from_env",
            return_value=docker_client,
        ):
            service = DockerSandboxService(config=config)
        labels, environment = service._build_labels_and_env(
            "owned-sandbox", request, None
        )
        token = labels["opensandbox.io/execd-access-token"]
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        self.assertEqual(labels["opensandbox.io/execd-isolation"], "enable")
        self.assertIn("EXECD_ACCESS_TOKEN=" + token, environment)
        self.assertNotIn("EXECD_ACCESS_TOKEN=config-token", environment)
        self.assertNotIn("EXECD_ACCESS_TOKEN=request-token", environment)
        second, _ = service._build_labels_and_env("another-sandbox", request, None)
        self.assertNotEqual(second["opensandbox.io/execd-access-token"], token)
        labels[SANDBOX_EMBEDDING_PROXY_PORT_LABEL] = "31000"
        labels[SANDBOX_HTTP_PORT_LABEL] = "31001"
        container = MagicMock()
        container.attrs = {"Config": {"Labels": labels}}
        with (
            patch.object(
                service, "_get_container_by_sandbox_id", return_value=container
            ),
            patch.object(service, "_extract_bridge_ip", return_value="172.20.0.7"),
            patch.object(
                service, "_resolve_public_host", return_value="public.example"
            ),
            patch.object(service, "_resolve_proxy_host", return_value="127.0.0.1"),
        ):
            for internal in (False, True):
                for port in (44772, 18080, 8080):
                    endpoint = service.get_endpoint(
                        "owned-sandbox", port, resolve_internal=internal
                    )
                    expected = port == 44772 or (not internal and port != 8080)
                    self.assertEqual(
                        (endpoint.headers or {}).get("X-EXECD-ACCESS-TOKEN"),
                        token if expected else None,
                    )
                    if internal:
                        forwarded = proxy._filter_proxy_headers(
                            {"X-EXECD-ACCESS-TOKEN": token, "User-Agent": "contract"},
                            endpoint.headers,
                            execd_target=proxy._uses_execd_proxy(endpoint, port),
                        )
                        self.assertEqual(
                            forwarded.get("X-EXECD-ACCESS-TOKEN"),
                            token if port == 44772 else None,
                        )
            labels[SANDBOX_EGRESS_AUTH_TOKEN_METADATA_KEY] = "egress-token"
            endpoint = service.get_endpoint(
                "owned-sandbox", 18080, resolve_internal=True
            )
            assert endpoint.headers is not None
            self.assertEqual(endpoint.headers["X-EXECD-ACCESS-TOKEN"], token)
            forwarded = proxy._filter_proxy_headers(
                {"X-EXECD-ACCESS-TOKEN": "manual-token"},
                {},
                execd_target=proxy._uses_execd_proxy(endpoint, 18080),
            )
            self.assertEqual(forwarded["X-EXECD-ACCESS-TOKEN"], "manual-token")
            labels.pop("opensandbox.io/execd-access-token")
            with self.assertRaises(HTTPException) as missing:
                service.get_endpoint("owned-sandbox", 44772)
            self.assertEqual(missing.exception.status_code, 500)

    def test_command_sandboxes_do_not_gain_reserved_authentication(self) -> None:
        config = AppConfig(
            runtime=RuntimeConfig(type="docker", execd_image="contract-execd"),
            docker=DockerConfig(network_mode="bridge"),
        )
        request = CreateSandboxRequest.model_validate(
            {
                "image": {"uri": "contract-runtime"},
                "resourceLimits": {},
                "entrypoint": ["/bin/sh"],
                "timeout": 120,
            }
        )
        with patch("opensandbox_server.services.docker.docker_service.docker.from_env"):
            service = DockerSandboxService(config=config)
        labels, environment = service._build_labels_and_env(
            "owned-sandbox", request, None
        )
        self.assertNotIn("opensandbox.io/execd-isolation", labels)
        self.assertFalse(
            any(value.startswith("EXECD_ACCESS_TOKEN=") for value in environment)
        )

    async def test_isolation_rejects_shared_parent_networks(self) -> None:
        """Refuse shared namespaces before any resources are provisioned."""
        for network_mode, driver, shared in (
            (None, "host", True),
            ("host", "host", True),
            ("container:external-container", "bridge", True),
            ("host-network-id", "host", True),
            ("project-network", "bridge", False),
            ("none", "null", False),
        ):
            for isolation in (None, "disable", "enable"):
                with self.subTest(network=network_mode, isolation=isolation):
                    docker_client = MagicMock()
                    docker_client.containers.list.return_value = []
                    docker_client.networks.get.return_value.attrs = {"Driver": driver}
                    docker_client.api.create_container.return_value = {"Id": "main-id"}
                    docker_config = (
                        DockerConfig()
                        if network_mode is None
                        else DockerConfig(network_mode=network_mode)
                    )
                    config = AppConfig(
                        runtime=RuntimeConfig(
                            type="docker", execd_image="opensandbox/execd:v1.0.22"
                        ),
                        docker=docker_config,
                    )
                    request = CreateSandboxRequest.model_validate(
                        {
                            "image": {"uri": "contract-runtime"},
                            "timeout": 120,
                            "resourceLimits": {},
                            "entrypoint": ["/bin/sh"],
                            "extensions": {}
                            if isolation is None
                            else {"bootstrap.execd.isolation": isolation},
                        }
                    )
                    with ExitStack() as patches:
                        patches.enter_context(
                            patch(
                                "opensandbox_server.services.docker.docker_service.docker.from_env",
                                return_value=docker_client,
                            )
                        )
                        service = DockerSandboxService(config=config)
                        for boundary in (
                            "_ensure_image_available",
                            "_prepare_sandbox_runtime",
                            "_schedule_expiration",
                        ):
                            patches.enter_context(patch.object(service, boundary))
                        patches.enter_context(
                            patch(
                                "opensandbox_server.services.docker.docker_service.allocate_port_bindings",
                                return_value={
                                    "44772": ("127.0.0.1", 44772),
                                    "8080": ("127.0.0.1", 8080),
                                },
                            )
                        )
                        if isolation == "enable" and shared:
                            with self.assertRaises(HTTPException) as rejected:
                                await service.create_sandbox(request)
                            self.assertEqual(rejected.exception.status_code, 400)
                            docker_client.api.create_container.assert_not_called()
                            docker_client.volumes.create.assert_not_called()
                            docker_client.images.pull.assert_not_called()
                        else:
                            await service.create_sandbox(request)
                            docker_client.api.create_container.assert_called_once()

    async def test_isolation_and_egress_permissions(self) -> None:
        """Retain defaults, egress routing, resource limits and no-new-privileges."""
        self.assertEqual(version("opensandbox-server"), "0.2.3")
        self.assertEqual(version("anyio"), "4.14.2")
        self.assertEqual(version("urllib3"), "2.8.0")
        for isolation in (None, "disable", "enable"):
            for egress, timeout in product((False, True), (120, None)):
                with self.subTest(isolation=isolation, egress=egress, timeout=timeout):
                    docker_client = MagicMock()
                    docker_client.containers.list.return_value = []
                    docker_client.api.create_container.return_value = {"Id": "main-id"}
                    docker_client.api.create_host_config.side_effect = lambda **kwargs: (
                        kwargs
                    )
                    docker_client.containers.get.return_value = MagicMock(id="main-id")
                    config = AppConfig(
                        runtime=RuntimeConfig(
                            type="docker", execd_image="opensandbox/execd:v1.0.22"
                        ),
                        docker=DockerConfig(
                            network_mode="bridge",
                            apparmor_profile="contract-apparmor",
                            seccomp_profile="contract-seccomp",
                            pids_limit=256,
                        ),
                        egress=EgressConfig(image="contract-egress"),
                    )
                    request = CreateSandboxRequest.model_validate(
                        {
                            "image": {"uri": "contract-runtime"},
                            "timeout": timeout,
                            "resourceLimits": {"memory": "512Mi", "cpu": "500m"},
                            "entrypoint": ["/bin/sh"],
                            "extensions": (
                                {}
                                if isolation is None
                                else {"bootstrap.execd.isolation": isolation}
                            ),
                            "networkPolicy": {"defaultAction": "deny", "egress": []}
                            if egress
                            else None,
                        }
                    )
                    with ExitStack() as patches:
                        patches.enter_context(
                            patch(
                                "opensandbox_server.services.docker.docker_service.docker.from_env",
                                return_value=docker_client,
                            )
                        )
                        service = DockerSandboxService(config=config)
                        for boundary in (
                            "_ensure_image_available",
                            "_prepare_sandbox_runtime",
                            "_schedule_expiration",
                        ):
                            patches.enter_context(patch.object(service, boundary))
                        patches.enter_context(
                            patch.object(
                                service,
                                "_start_egress_sidecar",
                                return_value=MagicMock(id="sidecar-id"),
                            )
                        )
                        patches.enter_context(
                            patch(
                                "opensandbox_server.services.docker.docker_service.allocate_port_bindings",
                                return_value={
                                    "44772": ("127.0.0.1", 44772),
                                    "8080": ("127.0.0.1", 8080),
                                    "18080": ("127.0.0.1", 18080),
                                },
                            )
                        )
                        await service.create_sandbox(request)
                    host_config = docker_client.api.create_container.call_args.kwargs[
                        "host_config"
                    ]
                    enabled = isolation == "enable"
                    self.assertEqual(
                        host_config.get("restart_policy"),
                        {"Name": "unless-stopped"} if timeout is None else None,
                    )
                    self.assertEqual(
                        set(host_config.get("cap_add", [])),
                        {"SYS_ADMIN", "NET_ADMIN"} if enabled else set(),
                    )
                    self.assertEqual(
                        set(host_config["cap_drop"]),
                        set(config.docker.drop_capabilities),
                    )
                    self.assertEqual(
                        host_config["security_opt"],
                        [
                            "no-new-privileges:true",
                            "apparmor=unconfined",
                            "seccomp=unconfined",
                        ]
                        if enabled
                        else [
                            "no-new-privileges:true",
                            "apparmor=contract-apparmor",
                            "seccomp=contract-seccomp",
                        ],
                    )
                    self.assertEqual(
                        host_config.get("tmpfs", {}),
                        {"/var/lib/execd/isolation": ""} if enabled else {},
                    )
                    self.assertFalse(host_config.get("privileged", False))
                    self.assertEqual(host_config["pids_limit"], 256)
                    self.assertEqual(host_config["mem_limit"], 512 * 1024 * 1024)
                    self.assertEqual(host_config["nano_cpus"], 500_000_000)
                    self.assertEqual(
                        host_config["network_mode"],
                        "container:sidecar-id" if egress else "bridge",
                    )
                    if egress:
                        self.assertNotIn("port_bindings", host_config)
                    else:
                        self.assertIn("port_bindings", host_config)
                    print(
                        json.dumps(
                            {
                                "isolation": isolation,
                                "egress": egress,
                                "host_config": host_config,
                            }
                        )
                    )

    def test_explicit_pause_survives_restart_and_resumes_the_same_instance(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            client = MagicMock()
            companion = MagicMock()
            companion.attrs = {
                "Config": {"Labels": {EGRESS_SIDECAR_LABEL: "existing"}},
                "State": {"Running": True},
            }
            client.containers.list.return_value = [companion]
            container = MagicMock(id="original-container")
            container.attrs = {
                "Config": {"Labels": {"opensandbox.io/manual-cleanup": "true"}},
                "State": {"Running": True, "Paused": False},
            }
            owner = PersistentSandboxLifecycle(client, Path(directory))
            owner.pause("existing", container)
            self.assertTrue(owner.is_paused("existing"))
            container.pause.assert_called_once()
            container.update.assert_called_with(restart_policy={"Name": "no"})
            companion.update.assert_called_with(restart_policy={"Name": "no"})

            # A new provider process sees exited containers after a daemon reboot.
            container.attrs["State"] = {"Running": False, "Paused": False}
            companion.attrs["State"] = {"Running": False}
            resumed_owner = PersistentSandboxLifecycle(client, Path(directory))
            resumed_owner.restore_pause("existing", container)
            container.start.assert_not_called()
            companion.start.assert_not_called()
            transitions = []
            companion.start.side_effect = lambda: transitions.append("network")
            container.start.side_effect = lambda: transitions.append("sandbox")
            resumed_owner.resume("existing", container)
            self.assertEqual(transitions, ["network", "sandbox"])
            self.assertFalse(resumed_owner.is_paused("existing"))
            container.update.assert_called_with(
                restart_policy={"Name": "unless-stopped"}
            )
            companion.update.assert_called_with(
                restart_policy={"Name": "unless-stopped"}
            )
            client.containers.create.assert_not_called()
            container.remove.assert_not_called()

    def test_retained_egress_companion_uses_the_parent_restart_policy(self) -> None:
        for retained in (False, True):
            with self.subTest(retained=retained):
                client = MagicMock()
                client.containers.list.return_value = []
                client.api.create_container.return_value = {"Id": "owned-network"}
                client.api.create_host_config.side_effect = lambda **kwargs: kwargs
                config = AppConfig(
                    runtime=RuntimeConfig(type="docker", execd_image="contract-execd"),
                    docker=DockerConfig(network_mode="bridge"),
                    egress=EgressConfig(image="contract-egress"),
                )
                request = CreateSandboxRequest.model_validate(
                    {
                        "image": {"uri": "contract-runtime"},
                        "timeout": None if retained else 120,
                        "resourceLimits": {},
                        "entrypoint": ["/bin/sh"],
                        "networkPolicy": {"defaultAction": "deny", "egress": []},
                    }
                )
                with patch(
                    "opensandbox_server.services.docker.docker_service.docker.from_env",
                    return_value=client,
                ):
                    service = DockerSandboxService(config=config)
                with patch.object(service, "_ensure_image_available"):
                    assert request.network_policy is not None
                    service._start_egress_sidecar(
                        "existing",
                        request.network_policy,
                        "test-owned-token",
                        44772,
                        8080,
                        persistent=retained,
                    )
                host = client.api.create_container.call_args.kwargs["host_config"]
                self.assertEqual(
                    host.get("restart_policy"),
                    {"Name": "unless-stopped"} if retained else None,
                )

    def test_manual_stop_without_pause_intent_is_never_started(self) -> None:
        with TemporaryDirectory() as directory:
            client = MagicMock()
            client.containers.list.return_value = []
            container = MagicMock()
            container.attrs = {
                "Config": {"Labels": {"opensandbox.io/manual-cleanup": "true"}},
                "State": {"Running": False, "Paused": False},
            }
            owner = PersistentSandboxLifecycle(client, Path(directory))
            owner.restore_pause("existing", container)
            with self.assertRaises(HTTPException) as captured:
                owner.resume("existing", container)
            self.assertEqual(captured.exception.status_code, 409)
            container.start.assert_not_called()
            container.update.assert_not_called()

    def test_startup_rechecks_pause_intent_after_concurrent_resume(self) -> None:
        with TemporaryDirectory() as directory:
            client = MagicMock()
            client.containers.list.return_value = []
            container = MagicMock()
            state = {"Running": True, "Paused": False}
            container.attrs = {
                "Config": {"Labels": {"opensandbox.io/manual-cleanup": "true"}},
                "State": state,
            }
            container.pause.side_effect = lambda: state.update(Paused=True)
            container.unpause.side_effect = lambda: state.update(Paused=False)
            owner = PersistentSandboxLifecycle(client, Path(directory))
            owner.pause("existing", container)
            inspected, release = Event(), Event()
            failures = []

            class Startup(PersistentSandboxLifecycle):
                def is_paused(self, sandbox_id: str) -> bool:
                    paused = super().is_paused(sandbox_id)
                    if paused and not inspected.is_set():
                        inspected.set()
                        release.wait()
                    return paused

            def restore() -> None:
                try:
                    Startup(client, Path(directory)).restore_pause(
                        "existing", container
                    )
                except BaseException as error:  # noqa: BLE001 - transfer worker failure to its owner
                    failures.append(error)

            worker = Thread(target=restore)
            worker.start()
            try:
                inspected.wait()
                owner.resume("existing", container)
            finally:
                release.set()
                worker.join()
            self.assertEqual(failures, [])
            self.assertFalse(owner.is_paused("existing"))
            self.assertFalse(state["Paused"])
            container.update.assert_called_with(
                restart_policy={"Name": "unless-stopped"}
            )

    def test_startup_defers_to_a_transition_owned_by_another_worker(self) -> None:
        with TemporaryDirectory() as directory:
            client = MagicMock()
            client.containers.list.return_value = []
            container = MagicMock()
            container.attrs = {
                "Config": {"Labels": {"opensandbox.io/manual-cleanup": "true"}},
                "State": {"Running": True, "Paused": False},
            }
            entered, release = Event(), Event()
            failures = []

            def pause_container() -> None:
                entered.set()
                release.wait()

            container.pause.side_effect = pause_container

            def pause() -> None:
                try:
                    PersistentSandboxLifecycle(client, Path(directory)).pause(
                        "existing", container
                    )
                except BaseException as error:  # noqa: BLE001 - transfer worker failure to its owner
                    failures.append(error)

            worker = Thread(target=pause)
            worker.start()
            try:
                entered.wait()
                container.update.reset_mock()
                PersistentSandboxLifecycle(client, Path(directory)).restore_pause(
                    "existing", container
                )
                container.update.assert_not_called()
            finally:
                release.set()
                worker.join()
            self.assertEqual(failures, [])

    def test_failed_resume_retains_pause_intent_for_reconciliation(self) -> None:
        with TemporaryDirectory() as directory:
            client = MagicMock()
            client.containers.list.return_value = []
            container = MagicMock()
            container.attrs = {"State": {"Running": True, "Paused": False}}
            owner = PersistentSandboxLifecycle(client, Path(directory))
            owner.pause("existing", container)
            container.attrs["State"] = {"Running": False, "Paused": False}
            container.start.side_effect = RuntimeError("test-owned start failure")
            with self.assertRaisesRegex(RuntimeError, "test-owned"):
                owner.resume("existing", container)
            self.assertTrue(owner.is_paused("existing"))
            container.update.assert_called_with(restart_policy={"Name": "no"})


if __name__ == "__main__":
    unittest.main()
