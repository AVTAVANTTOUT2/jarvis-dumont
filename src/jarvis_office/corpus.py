"""Human recording plan only; prompts are never claimed as verified transcripts."""

from pathlib import Path
from typing import Any

from jarvis_office.assets import AssetError, atomic_json, private_root

PROMPTS = (
    "Merci pour ta réponse, explique-moi la suite.",
    "L’une de mes questions concerne les camions.",
    "Ne ferme pas la porte, je reviens dans deux minutes.",
    "Je n’ai pas demandé de supprimer ce document.",
    "Quel est le nombre de camions disponibles aujourd’hui ?",
    "Le montant est de douze euros et cinquante centimes.",
    "Le rendez-vous est prévu le quinze septembre à neuf heures trente.",
    "Élodie et Benoît souhaitent une réponse demain matin.",
    "Peux-tu expliquer cette phrase sans changer son sens ?",
    "Il y a cent vingt-trois dossiers, et non trente-deux.",
    "Je voudrais connaître la prochaine étape, puis les deux suivantes.",
    "Nous partirons lundi, mais nous ne rentrerons pas mardi.",
    "La référence comprend les lettres A, B et C, suivies de vingt-quatre.",
    "Parlons de musique avant de reprendre notre conversation.",
    "Je marque une courte pause au milieu de cette phrase, puis je continue.",
    "Cette information est importante : le camion bleu reste au bureau.",
    "Pourquoi la date du premier octobre est-elle différente ?",
    "Je veux une réponse courte, avec un exemple concret et une conclusion.",
    "Il ne faut ni changer le nom, ni déplacer le fichier.",
    "Le numéro de téléphone commence par zéro six, puis douze et trente-quatre.",
    "La livraison attendue concerne trois camions, pas quatre.",
    "Est-ce que François a confirmé le rendez-vous du vingt et un novembre ?",
    "Je vais reformuler ma question après une pause pour préciser ce que je veux savoir.",
    "La température est de moins cinq degrés et la distance de dix kilomètres.",
    "Résume les trois points principaux et indique clairement ce qui reste inconnu.",
)


def prepare_human_corpus(output: Path) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise AssetError("corpus_manifest_already_exists")
    if not output.resolve().is_relative_to(private_root().resolve() / "corpus"):
        raise AssetError("corpus_manifest_must_be_private")
    rows = []
    for condition in ("quiet", "office_noise"):
        for n, prompt in enumerate(PROMPTS, 1):
            rows.append(
                {
                    "id": f"{condition}-{n:02d}",
                    "file": f"{condition}-{n:02d}.wav",
                    "prompt": prompt,
                    "reference": "",
                    "human_verified": False,
                    "source": "human",
                    "condition": condition,
                    "split": "holdout" if n >= 21 else "dev",
                    "critical_terms": [],
                }
            )
    atomic_json(
        output,
        {
            "schema_version": 1,
            "items": rows,
            "instructions": (
                "50 explicit recordings: 25 prompts in quiet and real office noise. "
                "Manually transcribe what was actually said into reference and set "
                "human_verified only after listening. Prompts are not ground truth. "
                "Keep the last five prompts in both conditions untouched for holdout."
            ),
            "business_vocabulary_to_supply": [],
            "human_validation": "BLOCKED_USER",
        },
    )
    return {
        "status": "PASS",
        "exit_code": 0,
        "planned_recordings": 50,
        "planned_holdout": 10,
        "audio_recorded": False,
        "human_validation": "BLOCKED_USER",
    }
