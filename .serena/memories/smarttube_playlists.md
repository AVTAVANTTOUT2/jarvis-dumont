# Playlists SmartTube

Worktree `jarvis-office-tv-final` (pas le checkout Echo).
Page dashboard Playlists, URLs YouTube persistées dans `preferences.tv_registry`.
Voix : « mets la playlist numéro N », « suivante » (utterance courte, pas « semaine suivante »).
Auto-avancement : lier `playback_id` depuis `playback_state` frais (SmartTube `play_content` reste `dispatched` sans id). Ignorer l'id pré-commande du même contenu. Avancer sur `ended` / `stopped` proche de la durée (observée, sinon yt-dlp) ; mémoriser le plus haut `position_ms` si SmartTube remet 0 à l'arrêt. Pause n'avance pas. Pas de minuteur mural.
Commandes courtes silencieuses : `pause`, `resume`, `stop`, `seek`, `playlist_next` renvoient `""` de `dispatch` quand appliquées ; `VoiceLoop._respond` intercepte `tv_speech == ""` pour sauter LLM et TTS, avorter l'audio, marquer PASS et notifier `on_turn_finished`. Les erreurs (STALE_TARGET, playlist vide, etc.) parlent toujours.
