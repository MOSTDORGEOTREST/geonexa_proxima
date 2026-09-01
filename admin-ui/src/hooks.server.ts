/**
 * Один интерцептор на всё приложение: истёкший access молча обновляется.
 *
 * Без него администратор каждые двенадцать часов попадал бы на форму входа
 * посреди работы, причём с потерей заполненной формы.
 */

import type { Handle } from '@sveltejs/kit';
import { api, ApiError } from '$lib/api/client';
import { ACCESS, REFRESH, clearSession, setSession } from '$lib/session';

type Tokens = { access_token: string; refresh_token: string; expires_in: number };

export const handle: Handle = async ({ event, resolve }) => {
	event.locals.token = event.cookies.get(ACCESS) ?? null;
	event.locals.username = null;

	if (!event.locals.token) {
		const refresh = event.cookies.get(REFRESH);
		if (refresh) {
			try {
				const tokens = await api<Tokens>('/api/admin/auth/refresh', {
					method: 'POST',
					body: { refresh_token: refresh },
					fetchImpl: event.fetch
				});
				setSession(event.cookies, tokens.access_token, tokens.refresh_token, tokens.expires_in);
				event.locals.token = tokens.access_token;
			} catch (error) {
				// Refresh мёртв — сессии нет. Чистим, чтобы не пытаться снова.
				if (error instanceof ApiError) clearSession(event.cookies);
			}
		}
	}

	return resolve(event);
};
