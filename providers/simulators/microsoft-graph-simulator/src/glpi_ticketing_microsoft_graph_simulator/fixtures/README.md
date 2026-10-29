# Fixtures audio synthétiques

Ces fichiers encodés contiennent uniquement la phrase fictive « Ceci est un
message vocal de test. », générée avec eSpeak NG puis convertie avec FFmpeg.
Ils ne contiennent aucune voix ni donnée réelle.

Les données sont compressées avec gzip puis encodées en Base64 afin de rester
des ressources textuelles. Le simulateur les décode en mémoire et vérifie leur
empreinte SHA-256 avant utilisation.

| Ressource décodée | Format | SHA-256 |
|---|---|---|
| `valid-speech.wav` | PCM signé 16 bits, mono, 16 kHz | `477a01ce6643d7f06ef4bd2e3564d4cb2e01f3fcfa8205e220185129c8889f7d` |
| `valid-speech-alaw.wav` | G.711 A-law, mono, 8 kHz | `86086f9f72d66f4b3b59f420e5dfd64a76880a0103a7cff99bfd1aaa7a1e611c` |
| `valid-speech.ogg` | OGG/Opus, mono | `0eb4e93632e8530feba90abbd1e48781ffc920684538bf3c5603d1c87942dedb` |
