"""Phase H7: the deployment files agree with what the code accepts (no Docker, no root needed)."""
import json
import os
import re
from pathlib import Path

from app.config import Settings

DEPLOY = Path(__file__).resolve().parents[2] / "deployment"


def _env_example() -> dict[str, str]:
    values = {}
    for line in (DEPLOY / "hornet.env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def _credentials() -> list[str]:
    unit = (DEPLOY / "hornet.service").read_text(encoding="utf-8")
    return re.findall(r"^LoadCredential=([^:]+):", unit, flags=re.M)


def test_the_example_env_with_credential_files_passes_the_prod_profile(tmp_path, monkeypatch):
    for key in [k for k in list(os.environ) if k.startswith("HORNET_") or k == "NVIDIA_API_KEY"]:
        monkeypatch.delenv(key)
    for key, value in _env_example().items():
        monkeypatch.setenv(key, value)
    fake = {"hornet_admin_api_key": "fake-admin-key-" + "0" * 32,
            "hornet_database_url": "postgresql+psycopg://hornet:fake@10.66.0.2:5432/hornet",
            "nvidia_api_key": "nvapi-fake-" + "0" * 20}
    assert set(_credentials()) == set(fake), "hornet.service credentials and the settings disagree"
    for name, value in fake.items():
        (tmp_path / name).write_text(value, encoding="utf-8")
    s = Settings(_env_file=None, _secrets_dir=tmp_path)
    assert s.env == "prod" and s.runtime_driver == "gvisor" and s.retention_auto
    assert s.admin_api_key == fake["hornet_admin_api_key"]
    assert s.database_url == fake["hornet_database_url"]
    assert s.nvidia_api_key is not None and s.nvidia_api_key.get_secret_value() == fake["nvidia_api_key"]
    assert not s.store_raw_commands and not s.gateway_log_passwords


def test_the_service_never_puts_secrets_in_its_environment():
    unit = (DEPLOY / "hornet.service").read_text(encoding="utf-8")
    assert not re.search(r"^Environment=.*(KEY|PASSWORD|DATABASE_URL)", unit, flags=re.M)
    assert not any(k.endswith(("API_KEY", "DATABASE_URL", "PASSWORD")) for k in _env_example())


def test_the_daemon_config_registers_runsc_without_network():
    conf = json.loads((DEPLOY / "docker-daemon.json").read_text(encoding="utf-8"))
    assert "--network=none" in conf["runtimes"]["runsc"]["runtimeArgs"]
    assert conf["icc"] is False and conf["no-new-privileges"] is True


def test_the_firewall_forwards_nothing_and_drops_by_default():
    rules = (DEPLOY / "nftables.conf").read_text(encoding="utf-8")
    assert len(re.findall(r"type filter hook (input|forward|output) priority filter; policy drop;", rules)) == 3
    forward = rules.split("chain forward", 1)[1].split("}", 1)[0]
    assert "accept" not in forward
