"""v0.5: causes, kinds of things with exceptions, how-to steps, multi-step questions."""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET

from polymath import qa
from polymath.interface.answer import Answer, Answerer
from polymath.perception import howto, semantic
from polymath.qa import chains
from polymath.senses.books_qa import se_document
from tests.world import World, geo_world


def text(answer: Answer) -> str:
    return " ".join(s.text for s in answer.statements) + " " + answer.note


def context(db, e1: int, e2: int, middle: str, sentence: str, doc: int = 0, right: str = ".") -> None:  # type: ignore[no-untyped-def]
    db.execute("INSERT INTO pair_contexts(e1, e2, doc_id, left_ctx, middle, right_ctx, sentence, created) "
               "VALUES(?,?,?,'',?,?,?,?)", (e1, e2, doc, middle, right, sentence, time.time()))  # fmt: skip


def bio_world(db) -> World:  # type: ignore[no-untyped-def]
    w = World(db)
    for key, label, *aliases in (("Q5113", "bird", "birds"), ("Q3238275", "penguin", "penguins"),
                                 ("Q729", "animal", "animals"), ("Q7377", "mammal", "mammals"),
                                 ("Q7380", "primate"), ("Q12078", "malaria"), ("Q1030", "mosquito", "mosquitoes"),
                                 ("Q662860", "smoking"), ("Q47912", "lung cancer"), ("Q12152", "vaccine"),
                                 ("Q878", "measles"), ("Q1001", "Great Depression"), ("Q1002", "stock market crash"),
                                 ("Q1003", "whale", "whales")):  # fmt: skip
        w.ent(key, label, *aliases)
    w.fact("penguin", "P279", "subclass of", o="bird")
    w.fact("bird", "P279", "subclass of", o="animal")
    w.fact("mammal", "P279", "subclass of", o="animal")
    w.fact("primate", "P279", "subclass of", o="mammal")
    w.fact("malaria", "P828", "has cause", o="mosquito")
    w.fact("Great Depression", "P828", "has cause", o="stock market crash")
    return w


# ------------------------------------------------------------------ causes and kinds from text
def test_cue_phrases_are_classified():
    assert semantic.classify("causes") == ("text:causes", False)
    assert semantic.classify("can cause") == ("text:causes", False)
    assert semantic.classify("is <det> major cause of") == ("text:causes", False)
    assert semantic.classify("was caused by") == ("text:causes", True)
    assert semantic.classify("due to") == ("text:causes", True)
    assert semantic.classify("prevents") == ("text:prevents", False)
    assert semantic.classify("is <det>") == ("text:is_a", False)
    assert semantic.classify("is <det> species of") == ("text:is_a", False)
    assert semantic.classify("and other") == ("text:is_a", False)
    assert semantic.classify("such as") == ("text:is_a", True)
    assert semantic.classify("was born in") is None and semantic.classify("") is None


def test_causes_and_kinds_are_read_from_sentences(db):
    w = bio_world(db)
    e = w.e
    context(db, e["smoking"], e["lung cancer"], "causes", "Smoking causes lung cancer.")
    context(db, e["smoking"], e["lung cancer"], "can cause", "Heavy smoking can cause lung cancer.")  # read twice
    context(db, e["measles"], e["vaccine"], "is prevented by", "Measles is prevented by the vaccine.")  # no cue
    context(db, e["vaccine"], e["measles"], "prevents", "The vaccine prevents measles.")
    context(db, e["vaccine"], e["measles"], "prevents", "A cheap vaccine prevents measles in children.")
    context(db, e["whale"], e["mammal"], "is <det>", "A whale is a mammal.")
    context(db, e["animal"], e["penguin"], "such as", "Animals such as penguins live in the cold.")
    context(db, e["malaria"], e["smoking"], "is <det>", "Malaria is a smoking.")  # smoking is no class
    context(
        db, e["mosquito"], e["malaria"], "causes", "The mosquito causes malaria cases to rise.", right="cases to rise."
    )
    stats = semantic.extract(db)
    assert stats == {"contexts": 9, "causes": 2, "prevents": 2, "kinds": 2, "remaining": 0}
    a = Answerer(db)
    assert "lung cancer can lead" not in text(a.ask("What does smoking cause?"))
    assert "smoking can lead to lung cancer" in text(a.ask("What does smoking cause?")).lower()
    assert "mosquito is a cause of malaria" in text(a.ask("What causes malaria?")).lower()
    assert (
        "stock market crash is a cause of great depression"
        in text(a.ask("Why did the Great Depression happen?")).lower()
    )
    assert "vaccine helps prevent measles" in text(a.ask("What prevents measles?")).lower()
    assert "not learned the causes of lung cancer" not in text(a.ask("What causes lung cancer?"))
    assert "have not learned the effects of malaria" in text(a.ask("What are the effects of malaria?"))
    assert "could not identify" in text(a.ask("What causes zorbification?"))
    assert "not learned what kind of thing whale is" in text(a.ask("Is a whale a mammal?"))  # read only once so far
    context(db, e["whale"], e["mammal"], "is <det>", "The blue whale is a mammal, not a fish.", right=", not a fish.")
    semantic.extract(db)
    assert text(a.ask("Is a whale a mammal?")).startswith("Yes. whale is a kind of mammal")
    # the job and its planner
    from polymath.core.scheduler import Scheduler

    agent = type("A", (), {"db": db, "scheduler": Scheduler(db)})()
    semantic.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'perception.semantic'") == 1
    ctx = type("C", (), {"db": db, "tick": staticmethod(lambda: None), "should_stop": staticmethod(lambda: False)})()
    assert semantic.semantic_job(ctx).result == {"causes": 0, "prevents": 0, "kinds": 0}


def test_what_kinds_can_do_with_exceptions(db):
    w = bio_world(db)
    e = w.e
    assert semantic.category_props("Birds can fly over long distances.", (0, 5, e["bird"])) == (e["bird"], "fly", 1)
    assert semantic.category_props("Penguins cannot fly.", (0, 8, e["penguin"])) == (e["penguin"], "fly", 0)
    assert semantic.category_props("Most mammals have hair.", (5, 12, e["mammal"])) == (e["mammal"], "have hair", 1)
    assert semantic.category_props("Birds lay eggs in nests.", (0, 5, e["bird"])) is None  # no modal: not a property
    assert semantic.category_props("They say birds can fly.", (9, 14, e["bird"])) is None  # not the subject
    assert semantic.category_props("Birds can be loud.", (0, 5, e["bird"])) is None
    assert semantic.category_props("Birds can fly.", None) is None
    assert semantic.verb_base("flies") == "fly" and semantic.verb_base("has wings") == "have wings"
    semantic.store_props(db, [(e["bird"], "fly", 1, 0, "Birds can fly."), (e["bird"], "fly", 1, 0, "Most birds can fly."),
                              (e["penguin"], "fly", 0, 0, "Penguins cannot fly."),
                              (e["mammal"], "have hair", 1, 0, "Mammals have hair.")])  # fmt: skip
    assert db.scalar("SELECT count FROM category_props WHERE entity_id = ? AND prop = 'fly'", (e["bird"],)) == 2
    a = Answerer(db)
    pen = text(a.ask("Can penguins fly?"))
    assert pen.startswith("No: penguin can") or pen.startswith("No: penguin cannot"), pen
    assert "an exception: bird generally can fly" in pen
    assert text(a.ask("Can birds fly?")).startswith("Yes")
    prim = text(a.ask("Do primates have hair?"))
    assert prim.startswith("Yes, as far as I know: mammal have hair") and "primate → mammal" in prim
    assert "have not read whether penguin can swim" in text(a.ask("Can penguins swim?"))
    assert qa.answer(a, "Do penguins sing?") is None  # nothing read: left to the text search
    kinds = text(a.ask("What kind of thing is a penguin?"))
    assert kinds.startswith("penguin is a bird (a kind of animal)")
    assert text(a.ask("Is a penguin an animal?")).startswith(
        "Yes. penguin is a kind of animal: penguin → bird → animal"
    )
    assert text(a.ask("Are penguins mammals?")).startswith("Not as far as I know")
    assert "not learned what kind of thing malaria is" in text(a.ask("What kind of thing is malaria?"))


# ------------------------------------------------------------------ how-to
ANSWER_HTML = (
    "<p>Debug mode is controlled by a single flag in the settings file, and the server only reads that file when it "
    "starts, which is why changing it while the server runs appears to do nothing at all. Here is the whole "
    "procedure that has worked for me on every machine I have tried it on so far:</p><ol><li>Open the <code>settings</code> file.</li><li>Set <b>debug</b> to true.</li>"
    "<li>Restart the server.</li></ol><p>That's it &amp; done.</p>"
)


def test_steps_are_read_from_answers():
    assert howto.steps_from_html(ANSWER_HTML) == [
        "Open the settings file.",
        "Set debug to true.",
        "Restart the server.",
    ]
    assert howto.steps_from_html("<ol><li>only one</li></ol>") == []
    assert howto.steps_from_text("Intro\n1. Install it\n2. Run it\n3) Enjoy\nThe end") == [
        "Install it",
        "Run it",
        "Enjoy",
    ]
    assert howto.steps_from_text("Step 1: do\nStep 2: done") == ["do", "done"]
    assert howto.steps_from_text("1. alone") == [] and howto.steps_from_text("") == []
    row = ET.fromstring(
        f'<row Id="7" PostTypeId="2" ParentId="3" Score="12" Body="{ANSWER_HTML.replace(chr(34), "&quot;").replace("<", "&lt;").replace(">", "&gt;")}" />'
    )
    doc = se_document(row, "superuser.com", {"3": "How do I enable debug mode?"})
    assert doc is not None and doc.meta["steps"][0] == "Open the settings file."


def test_how_to_questions_get_ordered_steps(db):
    from polymath.memory.documents import Document, DocumentStore

    store = DocumentStore(db)
    meta_q = {"kind": "question", "site": "superuser.com", "score": 5, "accepted": "7"}
    store.add(Document("stackexchange", "superuser.com:3", "How do I enable debug mode?", "How do I enable debug mode? " * 5,
                       "CC BY-SA 4.0", url="https://superuser.com/q/3", meta=meta_q))  # fmt: skip
    meta_a = {"kind": "answer", "site": "superuser.com", "score": 12, "parent": "3",
              "steps": ["Open the settings file.", "Set debug to true.", "Restart the server."]}  # fmt: skip
    store.add(Document("stackexchange", "superuser.com:7", "How do I enable debug mode?", "Do this. " * 30,
                       "CC BY-SA 4.0", url="https://superuser.com/a/7", meta=meta_a))  # fmt: skip
    a = Answerer(db)
    assert qa.answer(a, "How do I enable debug mode?") is None  # nothing filed yet
    assert howto.collect(db) == {"filed": 1, "remaining": 0}
    ans = a.ask("How do I enable debug mode?")
    assert "1. Open the settings file." in text(ans) and "3. Restart the server." in text(ans)
    assert "an accepted answer with score 12 on superuser.com" in ans.note
    assert ans.statements[0].citations[0].url == "https://superuser.com/a/7"
    assert qa.answer(a, "How do I bake bread?") is None  # nothing close: the general answerer tries
    from polymath.core.scheduler import Scheduler

    agent = type("A", (), {"db": db, "scheduler": Scheduler(db)})()
    howto.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'perception.howto'") == 1
    ctx = type("C", (), {"db": db, "tick": staticmethod(lambda: None), "should_stop": staticmethod(lambda: False)})()
    assert howto.howto_job(ctx).result == {"filed": 0}


# ------------------------------------------------------------------ multi-step
def test_multi_step_questions_follow_and_cite_each_hop(db):
    w = geo_world(db)
    a = Answerer(db)
    ans = a.ask("Who is the head of government of the capital of France?")
    assert ans.statements[0].text == "The capital of France is Paris."
    assert "The head of government of Paris is Anne Hidalgo." in text(ans)
    assert len(ans.statements[0].citations) >= 1
    river = text(a.ask("Which river flows through the capital of France?"))
    assert river.startswith("The capital of France is Paris. Rivers connected to Paris that way: Seine (flows through)")
    who = text(a.ask("Which country does Paris belong to the capital of?"))
    assert who  # an odd question still gets an answer object
    assert chains.resolve(a, "the capital of the country of Paris") is not None
    hop = chains.resolve(a, "the capital of France")
    assert hop is not None and hop[0].label == "Paris" and hop[1][0].relation == "capital"
    assert chains.resolve(a, "the capital of Narnia") is None
    not_yet = text(a.ask("What is the population of the capital of France?"))
    assert "but I have not learned the population of Paris yet" in not_yet
    w.fact("Berlin", "P6", "head of government", o="Anne Hidalgo")  # whatever: one more hop to Germany
    assert "Anne Hidalgo" in text(a.ask("Who is the head of government of the capital of Germany?"))
    assert qa.answer(a, "What is the capital of France?") is None  # one step: the plain answerer's job
    assert text(a.ask("Which city is the capital of France?")).startswith("The capital of France is Paris. So: Paris.")
    assert "not a river" in text(a.ask("Which river is the capital of France?"))
    one = a.ask("Who is the head of government of Paris?")  # a relation with "of" in its name
    assert one.statements[0].text == "The head of government of Paris is Anne Hidalgo."
