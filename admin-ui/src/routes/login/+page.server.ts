import { fail, redirect } from '@sveltejs/kit';
import { api, ApiError } from '$lib/api/client';
import { setSession } from '$lib/session';
import type { Actions, PageServerLoad } from './$types';

/** Куда возвращать после входа.
 *
 * Только внутрь админки. Без проверки параметр `next` — открытый редирект:
 * ссылка `/login?next=https://evil.example/login` уводит администратора на
 * чужой домен, где его ждёт та же форма входа. Протокол-относительный
 * `//evil.example` даёт то же самое, поэтому двойной слэш тоже отбрасываем.
 */
function safeNext(value: string | null): string {
	if (!value || !value.startsWith('/') || value.startsWith('//')) return '/';
	return value;
}

export const load: PageServerLoad = async ({ locals, url }) => {
	const next = safeNext(url.searchParams.get('next'));
	if (locals.token) redirect(303, next);
	return { next };
};

export const actions: Actions = {
	default: async ({ request, cookies, fetch }) => {
		const form = await request.formData();
		const username = String(form.get('username') ?? '').trim();
		const password = String(form.get('password') ?? '');
		const next = safeNext(String(form.get('next') ?? '/'));

		if (!username || !password) {
			return fail(400, { error: 'Введите логин и пароль', username });
		}

		try {
			const tokens = await api<{
				access_token: string;
				refresh_token: string;
				expires_in: number;
			}>('/api/admin/auth/login', {
				method: 'POST',
				body: { username, password },
				fetchImpl: fetch
			});
			setSession(cookies, tokens.access_token, tokens.refresh_token, tokens.expires_in);
		} catch (error) {
			if (error instanceof ApiError) {
				// 429 приходит от ограничителя попыток: сказать об этом честно
				// полезнее, чем повторять «неверный пароль».
				return fail(error.status === 429 ? 429 : 401, { error: error.message, username });
			}
			return fail(503, { error: 'API недоступен', username });
		}
		redirect(303, next);
	}
};
