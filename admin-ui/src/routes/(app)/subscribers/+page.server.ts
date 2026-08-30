import { api, tryApi } from '$lib/api/client';
import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, url, fetch }) => {
	const query = {
		page: url.searchParams.get('page') ?? 1,
		per_page: 50,
		q: url.searchParams.get('q') ?? undefined,
		kind: url.searchParams.get('kind') ?? undefined,
		status: url.searchParams.get('status') ?? undefined
	};
	const [list, breakdown] = await Promise.all([
		tryApi<any>('/api/admin/subscribers', { token: locals.token, query, fetchImpl: fetch }),
		tryApi<any>('/api/admin/subscribers/breakdown', { token: locals.token, fetchImpl: fetch })
	]);
	return { list, breakdown, filters: query };
};

export const actions: Actions = {
	approve: async ({ request, locals, fetch }) => run(request, locals, fetch, 'approve'),
	block: async ({ request, locals, fetch }) => run(request, locals, fetch, 'block')
};

async function run(request: Request, locals: App.Locals, fetchImpl: typeof fetch, verb: string) {
	const form = await request.formData();
	const id = String(form.get('id') ?? '');
	try {
		await api(`/api/admin/subscribers/${id}/${verb}`, {
			method: 'POST',
			token: locals.token,
			fetchImpl
		});
	} catch (error) {
		return fail(400, { error: (error as Error).message });
	}
	return { ok: true };
}
