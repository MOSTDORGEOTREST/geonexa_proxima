# Локальные модели

Эта директория предназначена только для локальных весов и исключена из Git.

```bash
hf download Qwen/Qwen3-Embedding-4B \
  --local-dir models/Qwen3-Embedding-4B

hf download Qwen/Qwen3-Reranker-0.6B \
  --local-dir models/Qwen3-Reranker-0.6B
```

Опциональный 8B embedder:

```bash
hf download Qwen/Qwen3-Embedding-8B \
  --local-dir models/Qwen3-Embedding-8B
```

Не коммитьте веса. FP16-параметры 4B требуют около 8 ГБ, 8B — около 16 ГБ,
причём фактическое потребление памяти будет выше. Зафиксируйте model revision и
проверьте лицензию перед production-использованием.
