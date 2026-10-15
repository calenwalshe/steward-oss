from steward.diagnosis import ClaudeDiagnoser
from steward.jev_observer import JevObserver
from steward.review import ClaudeTrajectoryReviewer
from steward.roles import ModelRoles, build_astra, build_jev, build_sol, load_roles
from steward.runner_exec import FakeRunner


def test_model_roles_defaults():
    roles = ModelRoles()
    assert roles.jev == "jev-latest"
    assert roles.sol == "sonnet"
    assert roles.astra == "opus"


def test_load_roles_returns_defaults_when_nothing_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path))
    for var in ("STEWARD_MODEL_JEV", "STEWARD_MODEL_SOL", "STEWARD_MODEL_ASTRA"):
        monkeypatch.delenv(var, raising=False)

    roles = load_roles()

    assert roles == ModelRoles()


def test_load_roles_reads_models_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path))
    for var in ("STEWARD_MODEL_JEV", "STEWARD_MODEL_SOL", "STEWARD_MODEL_ASTRA"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "models.toml").write_text('sol = "sonnet-custom"\nastra = "opus-custom"\n')

    roles = load_roles()

    assert roles.sol == "sonnet-custom"
    assert roles.astra == "opus-custom"
    assert roles.jev == "jev-latest"  # unset in file -> default


def test_env_overrides_take_precedence_over_the_toml_file(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path))
    (tmp_path / "models.toml").write_text('sol = "sonnet-custom"\n')
    monkeypatch.setenv("STEWARD_MODEL_SOL", "sonnet-env")
    monkeypatch.setenv("STEWARD_MODEL_ASTRA", "opus-env")
    monkeypatch.delenv("STEWARD_MODEL_JEV", raising=False)

    roles = load_roles()

    assert roles.sol == "sonnet-env"
    assert roles.astra == "opus-env"
    assert roles.jev == "jev-latest"


def test_build_sol_returns_a_claude_diagnoser_with_the_sol_model():
    runner = FakeRunner()
    diagnoser = build_sol(runner, ModelRoles(sol="sol-model"))

    assert isinstance(diagnoser, ClaudeDiagnoser)
    assert diagnoser.model == "sol-model"


def test_build_astra_returns_a_claude_trajectory_reviewer_with_the_astra_model():
    runner = FakeRunner()
    reviewer = build_astra(runner, ModelRoles(astra="astra-model"), timeout=42)

    assert isinstance(reviewer, ClaudeTrajectoryReviewer)
    assert reviewer.model == "astra-model"
    assert reviewer.timeout == 42


def test_build_jev_returns_a_jev_observer():
    observer = build_jev(ModelRoles(jev="jev-custom"))
    assert isinstance(observer, JevObserver)
