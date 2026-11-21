"""Names recognized in a game are also available to offline audits and later runs."""
import json
from pathlib import Path

from .storage import write_text_atomic


def _path(game: Path, language: str) -> Path:
    return game / "tl" / f".{language}.专名.json"


def read_name_groups(game: Path, language: str) -> dict[str, list[str]]:
    path = _path(game, language)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    if (not isinstance(data, dict)
            or any(not isinstance(v, list) or not all(isinstance(n, str) for n in v)
                   for v in data.values())):
        raise ValueError(f"专名记录格式无效：{path}")
    return data


def read_game_terms(game: Path, language: str) -> list[str]:
    groups = read_name_groups(game, language)
    return list(dict.fromkeys(name for group in groups.values() for name in group))


def save_game_terms(game: Path, language: str, characters, detected, previous) -> None:
    groups = {"characters": sorted(set(characters)), "detected": sorted(set(detected))}
    if groups != previous and (previous or any(groups.values())):
        write_text_atomic(_path(game, language), json.dumps(groups, ensure_ascii=False, indent=2))
        previous.update(groups)
