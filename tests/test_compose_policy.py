from scripts.validate_compose_policy import validate


def valid_model():
    return {
        "services": {
            "redis": {
                "command": [
                    "redis-server",
                    "--appendonly",
                    "yes",
                    "--appendfsync",
                    "everysec",
                    "--maxmemory",
                    "1gb",
                    "--maxmemory-policy",
                    "noeviction",
                    "--requirepass",
                    "ci-test-password",
                ]
            },
            "aitos-storage-maintenance": {
                "environment": {"DATA_DISK_MIN_FREE_GB": "2.5"}
            },
            "aitos-paper": {
                "image": "ghcr.io/example/aitos@sha256:" + "a" * 64,
                "volumes": [{"type": "volume", "target": "/models"}],
            },
            "aitos-live": {
                "image": "ghcr.io/example/aitos@sha256:" + "a" * 64,
                "volumes": [],
            },
        }
    }


def test_compose_policy_accepts_secure_merged_runtime_model():
    assert validate(valid_model()) == []


def test_compose_policy_rejects_replaced_redis_security_command():
    model = valid_model()
    model["services"]["redis"]["command"] = ["redis-server", "--appendonly", "yes"]
    errors = validate(model)
    assert any("--requirepass" in error for error in errors)
    assert any("--maxmemory" in error for error in errors)
    assert any("--maxmemory-policy" in error for error in errors)
    assert any("--appendfsync" in error for error in errors)


def test_compose_policy_rejects_storage_threshold_override():
    model = valid_model()
    model["services"]["aitos-storage-maintenance"]["environment"][
        "DATA_DISK_MIN_FREE_GB"
    ] = "20"
    assert any("DATA_DISK_MIN_FREE_GB" in error for error in validate(model))


def test_compose_policy_rejects_paper_source_mount_and_image_drift():
    model = valid_model()
    model["services"]["aitos-paper"]["volumes"].append(
        {"type": "bind", "source": ".", "target": "/app"}
    )
    model["services"]["aitos-live"]["image"] = "different:tag"
    errors = validate(model)
    assert any("same runtime image" in error for error in errors)
    assert any("bind-mount" in error for error in errors)
