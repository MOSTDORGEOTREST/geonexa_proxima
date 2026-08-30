# Админка Проксимы

SvelteKit + `adapter-node`. Токен администратора живёт в httpOnly-cookie и в
браузер не попадает: страницы собираются серверными `load`, которые ходят в
FastAPI по внутреннему адресу `API_INTERNAL_URL`. Наружу торчит только этот
node-слой.

```bash
npm install
API_INTERNAL_URL=http://127.0.0.1:8000 npm run dev     # разработка, :5173
npm run build && node build/index.js                   # production, :3000
```

Переменные окружения:

| переменная | зачем |
|---|---|
| `API_INTERNAL_URL` | адрес FastAPI изнутри сети (в compose — `http://api:8000`) |
| `ORIGIN` | внешний адрес админки; без него SvelteKit отвергает POST-формы как CSRF |
| `PORT` | порт node-сервера, по умолчанию 3000 |

Типы API генерируются из схемы, а не пишутся руками:

```bash
API_INTERNAL_URL=http://127.0.0.1:8000 npm run types
```

Дизайн-код — [../docs/design.md](../docs/design.md); шрифты и эмблема лежат в
`static/` и скопированы с сайта без изменений. Палитра данных объявлена в
`src/lib/charts/palette.ts`: слот закреплён за сущностью и приходит с бэкенда,
чтобы фильтр не перекрашивал оставшиеся серии.
