import json
import os
from datetime import datetime
from typing import Dict, List


class TopKDropoutTracker:
    def __init__(self, state_path: str, keep_k: int = 10, buffer_k: int = 20) -> None:
        self.state_path = state_path
        self.keep_k = keep_k
        self.buffer_k = buffer_k

    def update(self, horizon: str, ranked_rows: List[Dict[str, object]]) -> Dict[str, object]:
        state = self._load()
        horizon_state = state.get(horizon, {})
        previous_codes = horizon_state.get("codes", [])
        streaks = horizon_state.get("streaks", {})
        previous_set = set(previous_codes)
        ranked_codes = [row["code"] for row in ranked_rows[: self.buffer_k]]
        ranked_set = set(ranked_codes)

        retained = [code for code in previous_codes if code in ranked_set]
        entrants = [code for code in ranked_codes if code not in previous_set]
        stable_codes = (retained + entrants)[: self.keep_k]
        stable_set = set(stable_codes)
        dropped = [code for code in previous_codes if code not in stable_set]

        updated_streaks = {}
        for code in stable_codes:
            updated_streaks[code] = int(streaks.get(code, 0)) + 1

        row_by_code = {row["code"]: row for row in ranked_rows}
        stable_rows = []
        for rank, code in enumerate(stable_codes, start=1):
            row = dict(row_by_code[code])
            row["rank"] = rank
            row["stability_status"] = "retained" if code in previous_set else "new"
            row["streak"] = updated_streaks.get(code, 1)
            stable_rows.append(row)

        state[horizon] = {
            "codes": stable_codes,
            "streaks": updated_streaks,
            "last_updated": datetime.now().isoformat(timespec="seconds"),
        }
        self._save(state)
        return {
            "rows": stable_rows,
            "new_entries": entrants[: self.keep_k],
            "dropped": dropped,
            "retained": retained,
            "last_updated": state[horizon]["last_updated"],
        }

    def _load(self) -> Dict[str, object]:
        if not os.path.exists(self.state_path):
            return {}
        try:
            with open(self.state_path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, state: Dict[str, object]) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
