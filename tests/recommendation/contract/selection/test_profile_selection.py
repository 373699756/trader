"""Profile combinations share resources and keep training independent of downloads."""

import threading
from dataclasses import replace
from datetime import date, datetime, timedelta
from itertools import combinations
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.cold_start import _empty_config
from trader import bootstrap
from trader.download.domain.company_evidence import CompanyEvidenceInput, CompanyFinancial
from trader.download.domain.company_facts import CompileCompanyFacts
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.entrypoints.cli import BuildParser as CliParser
from trader.entrypoints.cli import Main as CliMain
from trader.entrypoints.server import BuildParser as ServerParser
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection
from trader.recommendation.domain.publication.models import ScoredDisposition, Strategy
from trader.training.entrypoints import commands, selected_training

_SELECTIONS = tuple(group for size in range(1, 5) for group in combinations(SCORING_PROFILE_IDS, size))


@pytest.mark.parametrize("profiles", _SELECTIONS)
def test_every_profile_combination_is_accepted_by_server_and_training(profiles):
    argument = ",".join(profiles)
    assert ParseProfileSelection(argument) == profiles
    assert ServerParser().parse_args([argument]).profiles == profiles
    assert CliParser().parse_args(["train", argument]).profiles == profiles


def test_omitted_selection_means_all_and_order_is_preserved():
    assert ServerParser().parse_args([]).profiles == SCORING_PROFILE_IDS
    assert CliParser().parse_args(["train"]).profiles == SCORING_PROFILE_IDS
    assert ParseProfileSelection("v5,v1,v2") == ("v5", "v1", "v2")


@pytest.mark.parametrize("selection", ("", "both", "v4", "v1,v1", "v2,", ",v3", "v1||v2", "v1|v2", "v1,,v5"))
def test_invalid_selection_is_rejected_before_any_runtime_work(selection):
    with pytest.raises(ValueError):
        ParseProfileSelection(selection)
    with pytest.raises(SystemExit) as caught:
        ServerParser().parse_args([selection])
    assert caught.value.code == 2
    with pytest.raises(SystemExit) as caught:
        CliParser().parse_args(["train", selection])
    assert caught.value.code == 2


@pytest.mark.parametrize("profiles", (("v1", "v2", "v3"), SCORING_PROFILE_IDS, ("v1", "v5"), ("v3",)))
def test_selected_profiles_share_resources_and_web_shows_only_selected_buttons(tmp_path, monkeypatch, profiles):
    config = _empty_config(tmp_path)
    monkeypatch.setattr(threading.Thread, "start", lambda self: pytest.fail("composition started a thread"))
    group = bootstrap.BuildProfiledSystem(config, profiles=profiles)
    default = "v2" if "v2" in profiles else profiles[0]
    primary = group.systems[0]
    assert primary.budget._file._path == tmp_path / ".runtime/trader/deepseek/state.json"
    assert primary.profile_id == default
    assert {system.profile_id for system in group.systems} == set(profiles)
    assert sum(system.owns_shared_resources for system in group.systems) == 1
    assert len({id(system.records) for system in group.systems}) == len(profiles)
    assert len({id(system.tomorrow_index) for system in group.systems}) == len(profiles)
    for system in group.systems:
        assert system.market_cache is primary.market_cache
        assert system.budget is primary.budget
        assert system.market_data.quotes.gateway is primary.market_data.quotes.gateway
        assert system.market_data.research is primary.market_data.research
        assert system.run_scope is primary.run_scope
        assert system.scheduler._dependencies.rounds is primary.scheduler._dependencies.rounds
    client = group.app.test_client()
    html = client.get("/").get_data(as_text=True)
    assert html.count('class="profile-tab') == len(profiles) + 1  # Includes the tablist class.
    profile_buttons = [line for line in html.splitlines() if 'data-profile="' in line]
    assert sum('aria-selected="true"' in line for line in profile_buttons) == 1
    for profile in SCORING_PROFILE_IDS:
        assert (f'data-profile="{profile}"' in html) == (profile in profiles)
        status = client.get(f"/api/status?profile={profile}")
        if profile in profiles:
            assert status.get_json()["scoring_profile"]["profile_id"] == profile
        else:
            assert status.status_code == 400
    assert f'name="trader-long-scoring-profile" content="{default}"' in html
    assert not (tmp_path / "data").exists()


def test_all_profiles_filter_the_shared_published_latest_report_without_using_old_profit(
    tmp_path, monkeypatch, application_feature_factory,
):
    observed_at = datetime(2026, 11, 8, 12, tzinfo=ZoneInfo("Asia/Shanghai"))

    def forbidden(*_args, **_kwargs):
        pytest.fail("company filter fixture must not start threads, request suppliers or train")

    monkeypatch.setattr(threading.Thread, "start", forbidden)
    monkeypatch.setattr(bootstrap.RequestsHttpTransport, "execute", forbidden)
    monkeypatch.setattr(bootstrap, "ExecuteSelectedTraining", forbidden)
    monkeypatch.setattr(bootstrap, "_utc_now", lambda: observed_at)
    monkeypatch.setattr(
        bootstrap.ChinaTradingCalendar, "open_dates",
        lambda self: (date(2026, 11, 6), date(2026, 11, 9)),
    )
    group = bootstrap.BuildProfiledSystem(_empty_config(tmp_path), profiles=SCORING_PROFILE_IDS)
    primary = group.systems[0]
    writer = SQLiteCompanyFactsIndex(
        primary.settings.data_paths.company,
        event_policy=primary.strategy.company_scoring.events,
        sqlite_policy=bootstrap.BuildSqlitePolicy(primary.settings),
    )
    writer.initialize()
    facts = []
    for code, old_parent, old_core, parent, core in (
        ("600001", 10, 8, -3, 2),
        ("600002", -10, -8, 3, 2),
        ("600003", -10, -8, 3, None),
    ):
        old = CompanyFinancial(
            code, date(2026, 3, 31), observed_at - timedelta(days=190),
            1, 5, 10, 10, 10, 10, old_parent, old_core,
        )
        latest = replace(
            old, report_date=date(2026, 6, 30), published_at=observed_at - timedelta(days=70),
            basic_eps=None, parent_net_profit=parent, core_net_profit=core,
        )
        source = CompanyEvidenceInput(
            code, observed_at, observed_at.date() - timedelta(days=365), observed_at.date(),
            "fixture", (), True, (old, latest), None, (), False, (), False, (),
        )
        facts.append(CompileCompanyFacts(source, primary.strategy.company_scoring.events))
    writer.publish(tuple(facts), {}, None, observed_at)
    codes = tuple(item.code for item in facts)
    research = primary.market_data.research
    research.initialize(codes, cancelled=lambda: False)
    assert research.status().entries == 3 and not research.status().last_error
    quotes = tuple(application_feature_factory(code, observed_at).quote for code in codes)

    assert {system.profile_id for system in group.systems} == set(SCORING_PROFILE_IDS)
    for system in group.systems:
        coordinator = system.scheduler._dependencies.data
        assert coordinator._market is system.market_data
        assert system.market_data.research is research
        assert coordinator._candidate_filtering.policy.hard_filter.structured_risk_thresholds[
            "latest_disclosed_report_loss"
        ] == 0
        observations = system.market_data.research.cached(codes, include_structured=True, as_of=observed_at)
        features = system.market_data.quotes._feature_builder.build(
            quotes, {}, observed_at, research_observations=observations,
        )
        assert {item.quote.code: item.values["latest_disclosed_report_loss"] for item in features} == {
            "600001": 1.0, "600002": 0.0, "600003": None,
        }
        plans = coordinator._candidate_filtering.plan(
            features, None, evaluated_at=observed_at, data_version="published-company-fixture",
            apply_model_eligibility=False,
        )
        assert set(plans.plans) == {Strategy.TOMORROW, Strategy.D25}
        for plan in plans.plans.values():
            evaluations = {item.code: item for item in plan.evaluations}
            assert evaluations["600001"].disposition is ScoredDisposition.REJECT
            assert "latest_disclosed_report_loss" in {
                item.code for item in evaluations["600001"].filter_reasons
            }
            assert "latest_disclosed_report_loss" not in {
                item.code for item in evaluations["600002"].filter_reasons
            }
            assert evaluations["600003"].disposition is ScoredDisposition.OBSERVE_ONLY
            missing = next(
                item for item in evaluations["600003"].optional_flags if item.code == "structured_risk_unavailable"
            )
            assert "latest_disclosed_report_loss" in missing.actual


@pytest.mark.parametrize("profiles", _SELECTIONS)
def test_every_profile_combination_shares_preview_authority_without_formal_initialization(
    tmp_path, monkeypatch, profiles
):
    config = _empty_config(tmp_path)
    now = datetime(2026, 10, 24, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(bootstrap, "_utc_now", lambda: now)
    monkeypatch.setattr(bootstrap.PublishedReferences, "initialize", lambda self: None)
    monkeypatch.setattr(
        bootstrap.ChinaTradingCalendar,
        "open_dates",
        lambda self: (now.date().replace(day=23), now.date().replace(day=26)),
    )
    group = bootstrap.BuildProfiledSystem(config, profiles=profiles)
    scope = group.systems[0].run_scope
    for system in group.systems:
        for initialize in system.supervisor._initializers:  # noqa: SLF001 - verifies actual composition guards
            initialize()
        assert system.run_scope is scope and scope.is_preview
        status = group.app.test_client().get(f"/api/status?profile={system.profile_id}").get_json()
        assert status["run_mode"] == "preview"
        assert status["market_trade_date"] == "2026-10-23"
        assert status["preview_run_id"] == scope.preview.run_id
        assert status["preview_state"] == "waiting_inputs"
        assert not (tmp_path / "data/recommendation" / system.profile_id).exists()
        assert not (tmp_path / "data/research" / system.profile_id).exists()
    allocated = [scope.next_sequence(lambda: pytest.fail("preview touched persistent sequence")) for _ in profiles]
    assert len(set(allocated)) == len(profiles)


@pytest.mark.parametrize("profiles", _SELECTIONS)
def test_training_selection_uses_local_history_and_runs_only_selected_profiles(
    tmp_path, monkeypatch, capsys, profiles
):
    config = _empty_config(tmp_path)
    calls = []

    def download(*_args, **_kwargs):
        pytest.fail("training must not prepare references or download")

    monkeypatch.setattr(bootstrap, "_prepare_reference", download)
    monkeypatch.setattr(bootstrap, "ExecuteSync", download)
    monkeypatch.setattr(bootstrap, "ExecuteHistoryDownload", download)
    monkeypatch.setattr(commands, "ConfigureTrainingResources", lambda *_args: calls.append("resources"))

    def train(
        runtime, selected, *, executor, memory_admission, cpu_budget, fanout_settings,
        sample_settings, paths, sqlite_policy, prepare_training, memory_capacity_bytes,
        load_expectation, started_at, terminal_columns, report_output,
    ):
        assert executor.status().workers == len(selected)
        assert sqlite_policy == bootstrap.BuildSqlitePolicy(runtime)
        assert cpu_budget.capacity <= cpu_budget.available_cpus
        assert fanout_settings.batch_rows > 0 and fanout_settings.queue_batches > 0
        assert sample_settings.sqlite_cache_bytes > 0 and sample_settings.v1_batch_rows > 0
        assert paths.history == runtime.data_paths.history
        assert paths.models == runtime.data_paths.models and paths.samples == runtime.data_paths.samples
        assert callable(prepare_training) and memory_capacity_bytes == memory_admission.capacity_bytes
        calls.extend(selected)
        return 0

    monkeypatch.setattr(selected_training, "RunSelectedTraining", train)
    assert CliMain(["--config", str(config), "train", ",".join(profiles)]) == 0
    assert calls[0] == "resources" and calls.count("resources") == 1
    assert sorted(calls[1:]) == sorted(profiles)
    captured = capsys.readouterr()
    assert "./run.sh sync history" in captured.err
    assert "正在准备官方总体" not in captured.err
    assert not (tmp_path / "data").exists()
