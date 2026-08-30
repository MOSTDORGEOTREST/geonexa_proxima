import { redirect } from '@sveltejs/kit';
import { tryApi } from '$lib/api/client';
import type { LayoutServerLoad } from './$types';

/** Гейт авторизации на всё дерево: забыть его на одном экране невозможно. */
export const load: LayoutServerLoad = async ({ locals, url, fetch }) => {
	if (!locals.token) {
		throw redirect(303, `/login?next=${encodeURIComponent(url.pathname + url.search)}`);
	}
	const me = await tryApi<{ username: string; environment: string }>('/api/admin/auth/me', {
		token: locals.token,
		fetchImpl: fetch
	});
	if (!me) throw redirect(303, '/login');
	return { me };
};
