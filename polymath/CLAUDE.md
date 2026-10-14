# Working on Polymath

Polymath is a from-scratch learning agent for Christian's Raspberry Pi 4. No LLMs, no external AI APIs, no
pretrained models. Runtime: the Python standard library and numpy only.

## The quality bar, every change

- **Code checks:** `ruff check .`, `ruff format --check .` and `mypy --strict polymath` are clean.
- **Tests:** `python -m pytest` passes; coverage stays at 85 % or more.
- **Docs:** update `README.md`, `docs/OPERATIONS.md`, `docs/ARCHITECTURE.md`, and `docs/VERIFICATION.md` (a new
  §12 entry with what was tested). Never claim anything was tested or deployed that was not.
- **Version:** bump `polymath/__init__.py` and `pyproject.toml` together.

## Keep the caretaker informed (Christian's standing rule)

Every update also updates the caretaker's knowledge, **`polymath/agents/faults.toml`**, in the same commit.
`tests/test_faults.py` fails otherwise. The caretaker is Orin (spec: `docs/CARETAKER.md`), and until Orin is
built the same knowledge drives `polymath caretaker`, `polymath doctor` and the issue inbox. Update it like this:

1. **`[meta] updated_for`** is the new version, and **`updated`** is today's date.
2. **An `[[update]]` entry** for the new version says what changed that the caretaker should know.
3. **`[situation]`** is what is true on the Pi right now:
   - what runs there;
   - what is broken;
   - what Christian still has to do;
   - the suspected root cause.
4. **A `[[fault]]` for every new way things can fail**, and every new `issues.report(db, area, code, …)` code
   matches one. Each fault needs:
   - symptoms, likely causes with priors (they add up to 1), and checks;
   - what Polymath does about it by itself;
   - steps, each with a `needs` tag. Anything that changes the system (boot settings, packages, permissions,
     formatting) is `needs = "approval"`.
5. **A `[[history]]` entry** for every problem seen on the real Pi: date, the version it ran, what happened, the
   cause, which fault, and `fixed_in`. Set `open = true` until it is confirmed fixed there.
6. If an existing fault's remedy changed, update its `steps` and `self_healing`, and set its `updated` to the new
   version.

## Hard rules (from Christian)

- **Never overwrite existing work.** Ask before destructive operations, privileged system changes, external
  side effects, or anything that widens security permissions.
- **The agent only learns.** It never runs commands, never touches other devices or sites, and vets every site
  before visiting it.
- **Self-improvement changes settings, never code.**
- **Never auto-format a drive that holds user files.**
- **Polymath must keep working whatever happens:**
  - every look at the brain's drive goes through `core/fsprobe`, which has a time limit;
  - every new failure path reports through `core/issues`.
