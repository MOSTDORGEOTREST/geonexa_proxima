import { tryApi } from '$lib/api/client';
import type { PageServerLoad } from './$types';

/** Параметры адресной строки, которые уходят в API как есть. */
const PASSTHROUGH = [
	'q',
	'source',
	'kind',
	'scored',
	'analyzed',
	'min_score',
	'date_from',
	'date_to',
	'created_from',
	'created_to',
	'language',
	'sort'
] as const;

export const load: PageServerLoad = async ({ locals, url, fetch }) => {
	const filters: Record<string, string> = {};
	for (const key of PASSTHROUGH) {
		const value = url.searchParams.get(key);
		if (value) filters[key] = value;
	}
	const page = Math.max(1, Number(url.searchParams.get('page') ?? 1) || 1);
	const query = { ...filters, page, per_page: 50 };
	const [list, facets] = await Promise.all([
		tryApi<any>('/api/admin/items', { token: locals.token, query, fetchImpl: fetch }),
		tryApi<any>('/api/admin/items/facets', { token: locals.token, fetchImpl: fetch })
	]);
	return { list, facets, filters, page };
};
