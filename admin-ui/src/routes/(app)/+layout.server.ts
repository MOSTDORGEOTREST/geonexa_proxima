import { redirect } from '@sveltejs/kit';
import { ApiError, api, tryApi } from '$lib/api/client';
import { clearSession } from '$lib/session';
import type { LayoutServerLoad } from './$types';

/** Гейт авторизации на всё дерево: забыть его на одном экране невозможно. */
export const load: LayoutServerLoad = async ({ locals, url, cookies, fetch }) => {
	if (!locals.token) {
		redirect(303, `/login?next=${encodeURIComponent(url.pathname + url.search)}`);
	}
	// `api`, а не `tryApi`: здесь важно различать «токен протух» и «API лежит».
	// Раньше обе ситуации давали редирект на /login, но cookie при этом
	// оставалась живой — а /login с живой cookie уводил обратно. Получалась
	// петля редиректов, из которой нельзя выйти даже кнопкой «выйти»: до неё
	// не доехать, она в шапке. Единственным лечением была ручная чистка cookie
	// в браузере.
	try {
		const me = await api<{ username: string; environment: string }>('/api/admin/auth/me', {
			token: locals.token,
			fetchImpl: fetch
		});
		// Счётчик заявок живёт в шапке: очередь, за которой нужно специально
		// заходить на отдельный экран, не разбирается никогда, а неподтверждённый
		// чат снаружи выглядит как сломанный бот.
		const queue = await tryApi<{ total: number }>('/api/admin/subscribers/pending', {
			token: locals.token,
			fetchImpl: fetch
		});
		return { me, pending: queue?.total ?? 0 };
	} catch (error) {
		if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
			// Сессия недействительна — гасим её и уводим на вход. Без чистки
			// cookie вход отфутболит обратно сюда.
			clearSession(cookies);
			redirect(303, `/login?next=${encodeURIComponent(url.pathname + url.search)}`);
		}
		// API недоступен — это не повод отправлять администратора на форму
		// входа: войти он всё равно не сможет, а причина не в нём.
		throw error;
	}
};
