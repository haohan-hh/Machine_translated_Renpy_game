"""Names recognized in a game are also available to offline audits and later runs."""
import json
from pathlib import Path

from .storage import write_text_atomic


# A title-cased English subject is not evidence of a name. These checks apply
# to suggestions and speaker labels only; explicit user terms always win.
_PRONOUNS = frozenset("""
    i me my mine myself you your yours yourself yourselves he him his himself
    she her hers herself it its itself we us our ours ourselves they them their
    theirs themself themselves all both everyone everybody somebody someone
""".split())
_ROLE_WORDS = frozenset("""
    captain chief doctor dr doc director lieutenant professor mr mrs ms miss
    sir lady lord king queen prince princess guard soldier officer receptionist
    scientist translator narrator stranger unknown mom dad mother father player
    protagonist man woman boy girl voice crowd nurse teacher assistant
""".split())
_AMBIGUOUS_ACRONYMS = frozenset(("IT", "US"))


def automatic_name_allowed(name: str) -> bool:
    trimmed = name.strip(" \t\r\n.,!?:;–—-")
    # IT / US can be actual acronyms; only the pronoun I is always all caps.
    if trimmed in _AMBIGUOUS_ACRONYMS:
        return True
    word = trimmed.casefold()
    return bool(word and any(ch.isalpha() for ch in word) and word not in _PRONOUNS)


def automatic_name_span(label: str) -> str:
    """Return the name itself, leaving generic roles, e.g. Captain in Captain Jo, translatable."""
    if not automatic_name_allowed(label) or any(c in label for c in "[{\\"):
        return ""  # Dynamic speaker expressions do not identify a literal name.
    words = label.split()
    while words and words[0].rstrip(".").casefold() in _ROLE_WORDS:
        words.pop(0)
    name = " ".join(words)
    if name != label and len(name) == 1 and name.isascii():
        return ""  # Scientist A, Guard 2, etc.
    return name if automatic_name_allowed(name) else ""


def required_game_names(characters, explicit=()) -> tuple[str, ...]:
    """Audit names backed by a speaker definition or expressly chosen by a user."""
    return tuple(dict.fromkeys(n for n in (*explicit, *map(automatic_name_span, characters)) if n))


def _path(game: Path, language: str) -> Path:
    return game / "tl" / f".{language}.专名.json"


def _read_groups(path: Path) -> dict[str, list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    if (not isinstance(data, dict)
            or any(not isinstance(v, list) or not all(isinstance(n, str) for n in v)
                   for v in data.values())):
        raise ValueError(f"专名记录格式无效：{path}")
    return data


def read_name_groups(game: Path, language: str) -> dict[str, list[str]]:
    data = _read_groups(_path(game, language))
    return {k: list(required_game_names(v)) if k in ("characters", "detected") else v
            for k, v in data.items()}


def read_game_terms(game: Path, language: str) -> list[str]:
    groups = read_name_groups(game, language)
    return list(dict.fromkeys(name for group in groups.values() for name in group))


def save_game_terms(game: Path, language: str, characters, detected, previous) -> None:
    groups = {**previous, "characters": sorted(required_game_names(characters)),
              "detected": sorted(required_game_names(detected))}
    if previous or any(groups.values()):
        path = _path(game, language)
        # read_name_groups filters legacy mistakes without writing during an
        # audit; the next normal save must also retire them on disk.
        if groups != _read_groups(path):
            write_text_atomic(path, json.dumps(groups, ensure_ascii=False, indent=2))
        previous.update(groups)
