"""Quota planning: turn category shares into per-dataset limits."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import plan_sampling  # noqa: E402


# -- allocation --------------------------------------------------------------


def test_allocation_is_proportional_to_pool_size():
    assert plan_sampling.allocate(10000, {"a": 50000, "b": 30000, "c": 20000}) == {
        "a": 5000,
        "b": 3000,
        "c": 2000,
    }


def test_allocation_never_exceeds_a_pool():
    pools = {"big": 50000, "small": 300, "tiny": 200}

    allocation = plan_sampling.allocate(10000, pools)

    assert sum(allocation.values()) == 10000
    assert all(allocation[name] <= pools[name] for name in allocation)


def test_a_pool_below_its_proportional_share_just_gets_less():
    # 'small' deserves 20000 * 100/50100 = 40, well inside its capacity.
    allocation = plan_sampling.allocate(20000, {"big": 50000, "small": 100})

    assert allocation == {"big": 19960, "small": 40}


def test_allocation_redistributes_once_a_pool_is_capped():
    # Asking for nearly everything forces 'small' to its ceiling; the shortfall
    # has to be taken up by the pool that still has room.
    pools = {"big": 50000, "small": 100}

    allocation = plan_sampling.allocate(50050, pools)

    assert allocation["small"] == 100
    assert allocation["big"] == 49950
    assert sum(allocation.values()) == 50050


def test_allocation_caps_at_what_is_available():
    allocation = plan_sampling.allocate(10000, {"a": 3000, "b": 2000})

    assert sum(allocation.values()) == 5000
    assert allocation == {"a": 3000, "b": 2000}


def test_allocation_of_zero_target_is_empty():
    assert plan_sampling.allocate(0, {"a": 100}) == {}


# -- matching ----------------------------------------------------------------


def test_patterns_and_exact_names_both_match():
    names = ["textcaps", "sharegpt4v(coco)", "vqav2", "docvqa"]

    matched = plan_sampling.match_datasets(
        names, {"match": ["vqav2"], "patterns": ["caption|^sharegpt4[ov]", "caps$"]}
    )

    assert sorted(matched) == ["sharegpt4v(coco)", "textcaps", "vqav2"]


def test_matching_is_case_insensitive_and_tolerates_punctuation():
    names = ["CoSyn_400k_chart", "lrv_normal(filtered)", "mapqa(mathv360k)"]

    matched = plan_sampling.match_datasets(names, {"patterns": ["^cosyn", r"\(filtered\)$"]})

    assert sorted(matched) == ["CoSyn_400k_chart", "lrv_normal(filtered)"]


# -- end to end --------------------------------------------------------------


@pytest.fixture()
def fake_ledger(monkeypatch):
    pools = {
        "textcaps": 100000,
        "sharegpt4v(coco)": 80000,
        "vqav2": 60000,
        "docvqa": 20000,
        "some_unmapped_set": 5000,
    }
    monkeypatch.setattr(plan_sampling, "available_rows", lambda config, statuses: pools)
    return pools


def test_plan_writes_per_dataset_limits(tmp_path, fake_ledger, capsys):
    base = tmp_path / "base.json"
    base.write_text(
        json.dumps(
            {
                "dataset_registry": "configs/datasets.json",
                "datasets": ["*"],
                "output_jsonl": "output/train.jsonl",
                "limit_per_dataset": 100,
                "mysql": {"host": "h", "user": "u", "database": "d"},
            }
        ),
        encoding="utf-8",
    )
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "total": 10000,
                "categories": {
                    "caption": {"share": 0.6, "patterns": ["caps$", "^sharegpt4v"]},
                    "vqa": {"share": 0.4, "patterns": ["vqa"]},
                },
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.json"

    code = plan_sampling.main(
        ["--config", str(base), "--plan", str(plan), "-o", str(out)]
    )
    written = json.loads(out.read_text(encoding="utf-8"))
    by_name = {item["name"]: item["limit"] for item in written["datasets"]}

    assert code == 0
    assert sum(by_name.values()) == 10000
    # The blanket cap must go, or it would silently override the per-dataset ones.
    assert "limit_per_dataset" not in written
    assert written["chinese_ratio"] == 1.0
    assert all(item["chinese_ratio"] == 1.0 for item in written["datasets"])
    # The mysql section carries over untouched.
    assert written["mysql"]["database"] == "d"
    # Unmapped datasets are excluded, and reported rather than dropped silently.
    assert "some_unmapped_set" not in by_name
    assert "some_unmapped_set" in capsys.readouterr().out


def test_a_dataset_is_only_claimed_by_the_first_matching_category(tmp_path, fake_ledger):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"datasets": ["*"], "mysql": {}}), encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "total": 1000,
                "categories": {
                    "first": {"share": 0.5, "patterns": ["vqa"]},
                    "second": {"share": 0.5, "patterns": ["vqa"]},
                },
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.json"

    plan_sampling.main(["--config", str(base), "--plan", str(plan), "-o", str(out)])
    written = json.loads(out.read_text(encoding="utf-8"))

    # Each dataset appears once; double-counting would inflate the real total.
    names = [item["name"] for item in written["datasets"]]
    assert len(names) == len(set(names))


def test_dump_lists_datasets_without_a_plan(tmp_path, fake_ledger, capsys):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"mysql": {}}), encoding="utf-8")

    code = plan_sampling.main(["--config", str(base), "--dump"])
    out = capsys.readouterr().out

    assert code == 0
    assert "textcaps" in out and "5 datasets" in out


def test_max_share_stops_one_dataset_swamping_a_category():
    # Without a cap the million-row set would supply almost the whole quota.
    pools = {"huge": 1_000_000, "small_a": 50_000, "small_b": 50_000}

    uncapped = plan_sampling.allocate(10000, pools)
    capped = plan_sampling.allocate(10000, pools, max_share=0.3)

    assert uncapped["huge"] > 9000
    assert capped == {"huge": 3000, "small_a": 3000, "small_b": 3000}


def test_a_cap_too_tight_for_the_member_count_cannot_fill_the_target():
    # Three datasets capped at 30% each can only ever supply 90%; the planner
    # reports the shortfall rather than quietly overshooting the cap.
    capped = plan_sampling.allocate(
        10000, {"a": 999999, "b": 999999, "c": 999999}, max_share=0.3
    )

    assert sum(capped.values()) == 9000


def test_max_share_still_fills_the_target_when_others_can_cover():
    capped = plan_sampling.allocate(10000, {"a": 999999, "b": 999999}, max_share=0.6)

    assert sum(capped.values()) == 10000
    assert max(capped.values()) <= 6000


# -- counts from a file ------------------------------------------------------


def test_counts_are_read_with_separators_and_parenthesised_names(tmp_path):
    path = tmp_path / "counts.txt"
    path.write_text(
        "# pasted straight out of a terminal\n"
        " 1,665,847  objects365_qa\n"
        "   263,581  visualwebinstruct(filtered)\n"
        "\n"
        "       194  funsd\n",
        encoding="utf-8",
    )

    assert plan_sampling.read_counts(path) == {
        "objects365_qa": 1665847,
        "visualwebinstruct(filtered)": 263581,
        "funsd": 194,
    }


def test_a_line_that_is_not_a_count_names_the_file_and_line(tmp_path):
    path = tmp_path / "counts.txt"
    path.write_text("100 okvqa\nnot-a-number chartqa\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"counts.txt:2: 'not-a-number' is not a row count"):
        plan_sampling.read_counts(path)


def test_a_line_without_a_dataset_name_is_rejected(tmp_path):
    path = tmp_path / "counts.txt"
    path.write_text("100\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"counts.txt:1: expected"):
        plan_sampling.read_counts(path)


# -- cap mode ----------------------------------------------------------------


def write_plan(tmp_path, categories):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"total": 100, "categories": categories}), encoding="utf-8")
    return path


def test_cap_mode_takes_whole_datasets_up_to_the_cap(tmp_path, capsys):
    counts = tmp_path / "counts.txt"
    counts.write_text("500000 big\n30000 small\n", encoding="utf-8")
    plan = write_plan(tmp_path, {"all": {"share": 1.0, "match": ["big", "small"]}})
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"dataset_registry": "r.json", "limit_per_dataset": 9}), encoding="utf-8")
    output = tmp_path / "out.json"

    code = plan_sampling.main([
        "--counts", str(counts), "--config", str(base), "--plan", str(plan),
        "--cap", "100000", "--chinese-ratio", "1.0", "-o", str(output),
    ])

    assert code == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["datasets"] == [
        {"name": "big", "limit": 100000, "chinese_ratio": 1.0},
        {"name": "small", "limit": 30000, "chinese_ratio": 1.0},
    ]
    # The per-dataset cap replaces the blanket limit, which must not linger.
    assert "limit_per_dataset" not in written
    assert "1/2 个数据集被完整翻译" in capsys.readouterr().out


def test_cap_mode_excludes_datasets_no_category_lists(tmp_path, capsys):
    counts = tmp_path / "counts.txt"
    counts.write_text("100 kept\n200 unlisted\n", encoding="utf-8")
    plan = write_plan(tmp_path, {"all": {"share": 1.0, "match": ["kept"]}})
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"dataset_registry": "r.json"}), encoding="utf-8")
    output = tmp_path / "out.json"

    plan_sampling.main([
        "--counts", str(counts), "--config", str(base), "--plan", str(plan),
        "--cap", "1000", "-o", str(output),
    ])

    assert [item["name"] for item in json.loads(output.read_text(encoding="utf-8"))["datasets"]] == ["kept"]
    assert "unlisted" in capsys.readouterr().out


def test_a_dataset_matched_by_two_categories_is_only_claimed_once(tmp_path):
    counts = tmp_path / "counts.txt"
    counts.write_text("100 shared\n", encoding="utf-8")
    plan = write_plan(tmp_path, {
        "first": {"share": 0.5, "match": ["shared"]},
        "second": {"share": 0.5, "match": ["shared"]},
    })
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"dataset_registry": "r.json"}), encoding="utf-8")
    output = tmp_path / "out.json"

    plan_sampling.main([
        "--counts", str(counts), "--config", str(base), "--plan", str(plan),
        "--cap", "1000", "-o", str(output),
    ])

    assert len(json.loads(output.read_text(encoding="utf-8"))["datasets"]) == 1
