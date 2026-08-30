import { fail, redirect } from '@sveltejs/kit';
import { api, ApiError } from '$lib/api/client';
import { setSession } from '$lib/session';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, url }) => {
	if (locals.token) throw redirect(303, url.searchParams.get('next') ?? '/');
	return { next: url.searchParams.get('next') ?? '/' };
};

export const actions: Actions = {
	default: async ({ request, cookies, fetch }) => {
		const form = await request.formData();
		const username = String(form.get('username') ?? '').trim();
		const password = String(form.get('password') ?? '');
		const next = String(form.get('next') ?? '/');

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
		throw redirect(303, next);
	}
};
