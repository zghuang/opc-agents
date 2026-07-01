from __future__ import annotations

from delivery.config_validation import config_validation_summary, validate_project_config_files


def test_validate_project_config_files_detects_unquoted_inline_mapping(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """services:
  redpanda:
    image: redpandadata/redpanda:v24.3.13
    command:
      - redpanda
      - start
      - --set
      - redpanda.kafka_api=[{address: "0.0.0.0", port: 9092}]
""",
        encoding="utf-8",
    )

    failures = validate_project_config_files(tmp_path)

    assert failures
    assert failures[0]["path"] == "docker-compose.yml"
    assert "line" in failures[0]["error"]
    assert "docker-compose.yml" in config_validation_summary(failures)


def test_validate_project_config_files_accepts_quoted_inline_mapping(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """services:
  redpanda:
    image: redpandadata/redpanda:v24.3.13
    command:
      - redpanda
      - start
      - --set
      - 'redpanda.kafka_api=[{address: "0.0.0.0", port: 9092}]'
""",
        encoding="utf-8",
    )

    assert validate_project_config_files(tmp_path) == []
