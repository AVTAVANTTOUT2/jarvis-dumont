# Playlists SmartTube

Worktree `jarvis-office-tv-final` (pas le checkout Echo).
Page dashboard Playlists, URLs YouTube persistées dans `preferences.tv_registry`.
Voix : « mets la playlist numéro N », « suivante » (utterance courte, pas « semaine suivante »).
Auto-avancement : lier `playback_id` depuis `playback_state` frais (SmartTube `play_content` reste `dispatched` sans id). Ignorer l'id pré-commande du même contenu. Avancer sur `ended` / `stopped` proche de la durée (observée, sinon yt-dlp) ; mémoriser le plus haut `position_ms` si SmartTube remet 0 à l'arrêt. Pause n'avance pas. Pas de minuteur mural.
Commandes courtes silencieuses : `pause`, `resume`, `stop`, `seek`, `playlist_next` renvoient `""` de `dispatch` quand appliquées. En Echo Commandes seulement, `handle_command` / `dispatch_command` joue un son OK ou erreur via PCM, sans LLM conversationnel ni TTS. Conversation n'appelle plus le dispatcher TV.
