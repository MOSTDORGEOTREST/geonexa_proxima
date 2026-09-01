import { tryApi } from '$lib/api/client';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, fetch }) => {
	// Текст берётся из API, а не набирается здесь: тот же самый показывает бот
	// по команде /howto, и две копии одной инструкции однажды разойдутся.
	const guide = await tryApi<any>('/api/admin/profiles/guide', {
		token: locals.token,
		fetchImpl: fetch
	});
	return { guide };
};
