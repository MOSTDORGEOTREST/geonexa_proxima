import { redirect } from '@sveltejs/kit';
import { tryApi } from '$lib/api/client';
import { clearSession } from '$lib/session';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = async ({ locals, cookies, fetch }) => {
	// Сервер не хранит выданные токены, поэтому «отзыв» — это запись в аудит
	// и удаление cookie. Делать вид, что токен аннулирован, было бы неправдой.
	await tryApi('/api/admin/auth/logout', {
		method: 'POST',
		token: locals.token,
		fetchImpl: fetch
	});
	clearSession(cookies);
	throw redirect(303, '/login');
};
