from pathlib import Path


def test_payload_reading_workers_mount_raw_payload_volume() -> None:
    compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")

    for service_name in ("api", "paper-worker", "experiment-worker", "task-worker"):
        block = _service_block(compose_text, service_name)

        assert "RAW_PAYLOAD_DIR: /var/lib/hfd/raw_payloads" in block
        assert 'EXTERNALIZE_RAW_PAYLOADS: "true"' in block
        assert "- hfd_raw_payloads:/var/lib/hfd/raw_payloads" in block


def _service_block(compose_text: str, service_name: str) -> str:
    lines = compose_text.splitlines()
    start = next(index for index, line in enumerate(lines) if line == f"  {service_name}:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
            end = index
            break
    return "\n".join(lines[start:end])
