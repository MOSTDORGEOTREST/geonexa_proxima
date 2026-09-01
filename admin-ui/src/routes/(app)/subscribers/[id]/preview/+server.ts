import { api } from '$lib/api/client';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

/**
 * Предпросмотр разбиения черновика на темы.
 *
 * Прокси, а не прямой запрос из браузера: токен живёт в httpOnly-cookie и в
 * браузер не попадает, поэтому в API ходит node-слой. И разбиение считает
 * Python — тот же код, который потом будет искать; повторить правила разбора
 * на TypeScript значило бы гарантированно с ним разъехаться.
 */
export const POST: RequestHandler = async ({ request, locals, fetch }) => {
	const body = await request.json().catch(() => ({}));
	try {
		const result = await api<unknown>('/api/admin/profiles/preview', {
			method: 'POST',
			token: locals.token,
			body: {
				description: String(body?.description ?? ''),
				// Явные темы берутся из сохранённого профиля: без них список тем
				// в предпросмотре короче того, по которому пойдёт поиск.
				profile_id: body?.profile_id ?? null
			},
			fetchImpl: fetch
		});
		return json(result);
	} catch (error) {
		return json({ error: (error as Error).message }, { status: 502 });
	}
};
