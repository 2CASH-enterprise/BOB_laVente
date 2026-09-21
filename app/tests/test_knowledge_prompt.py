from app.agents.prompts import build_system_prompt
from app.models.knowledge_entry import KnowledgeCategory, KnowledgeEntry
from app.models.tenant import Tenant


def _make_tenant():
    return Tenant(name="Boutique Test", country="SN", currency="XOF")


def test_prompt_without_knowledge_entries_has_no_empty_section():
    prompt = build_system_prompt(_make_tenant(), [])
    assert "BASE DE CONNAISSANCES" not in prompt


def test_prompt_includes_objection_entries():
    entries = [
        KnowledgeEntry(category=KnowledgeCategory.OBJECTION, title="C'est trop cher", content="Proposer le modèle économique A36, moins cher.", active=True),
    ]
    prompt = build_system_prompt(_make_tenant(), entries)
    assert "BASE DE CONNAISSANCES" in prompt
    assert "Réponses aux objections courantes" in prompt
    assert "C'est trop cher" in prompt
    assert "modèle économique A36" in prompt


def test_prompt_groups_entries_by_category():
    entries = [
        KnowledgeEntry(category=KnowledgeCategory.HORAIRES, title="Horaires", content="9h-18h du lundi au samedi", active=True),
        KnowledgeEntry(category=KnowledgeCategory.LIVRAISON, title="Délai", content="48h à Dakar", active=True),
    ]
    prompt = build_system_prompt(_make_tenant(), entries)
    assert "Horaires" in prompt
    assert "Livraison" in prompt
    assert "9h-18h" in prompt
    assert "48h à Dakar" in prompt
