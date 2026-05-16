from pathlib import Path


def test_payload_reading_workers_mount_raw_payload_volume() -> None:
    compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")

    for service_name in ("api", "paper-worker", "experiment-worker", "task-worker"):
        block = _service_block(compose_text, service_name)

        assert "RAW_PAYLOAD_DIR: /var/lib/hfd/raw_payloads" in block
        assert 'EXTERNALIZE_RAW_PAYLOADS: "true"' in block
        assert "- hfd_raw_payloads:/var/lib/hfd/raw_payloads" in block


def test_db_init_mounts_current_migration_tree() -> None:
    compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")
    block = _service_block(compose_text, "db-init")

    assert "- ./app:/app/app:ro" in block
    assert "- ./migrations:/app/migrations:ro" in block
    assert "- ./alembic.ini:/app/alembic.ini:ro" in block


def test_postgres_has_shared_memory_for_research_reports() -> None:
    compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")
    block = _service_block(compose_text, "postgres")

    assert "shm_size: 512m" in block
    assert "max_parallel_workers_per_gather=0" in block
    assert "work_mem=16MB" in block


def test_experiment_worker_uses_guarded_research_report_interval() -> None:
    compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")
    block = _service_block(compose_text, "experiment-worker")

    assert "--research-report-interval-seconds" in block
    assert '      - "3600"' in block
    assert "--research-report-limit" in block
    assert '      - "5000"' in block


def test_experiment_worker_maintains_core_darkflow_pipeline() -> None:
    compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")
    block = _service_block(compose_text, "experiment-worker")

    assert "--darkflow-interval-seconds" in block
    assert '      - "900"' in block
    assert "--darkflow-limit" in block
    assert "--darkflow-backtest-limit" in block
    assert "--darkflow-candidate-limit" in block
    assert "--darkflow-shadow-limit" in block


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
