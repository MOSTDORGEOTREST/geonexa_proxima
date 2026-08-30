import adapter from '@sveltejs/adapter-node';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
export default {
  preprocess: vitePreprocess(),
  kit: {
    adapter: adapter(),
    // Токен живёт в httpOnly-cookie и наружу не выходит: браузер ходит только
    // в node-слой, а тот — в FastAPI по внутреннему адресу. Проверка Origin на
    // POST включена по умолчанию; список доверенных источников берётся из
    // ORIGIN, который адаптеру всё равно обязателен.
  }
};
