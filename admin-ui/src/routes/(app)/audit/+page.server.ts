import { tryApi } from '$lib/api/client';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, url, fetch }) => {
	const log = await tryApi<any>('/api/admin/audit', {
		token: locals.token,
		query: { page: url.searchParams.get('page') ?? 1, action: url.searchParams.get('action') ?? undefined },
		fetchImpl: fetch
	});
	return { log };
};
