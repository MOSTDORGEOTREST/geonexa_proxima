import { api, tryApi } from '$lib/api/client';
import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, fetch }) => {
	const [providers, models, roles, usage] = await Promise.all([
		tryApi<any[]>('/api/admin/llm/providers', { token: locals.token, fetchImpl: fetch }),
		tryApi<any[]>('/api/admin/llm/models', { token: locals.token, fetchImpl: fetch }),
		tryApi<any[]>('/api/admin/llm/roles', { token: locals.token, fetchImpl: fetch }),
		tryApi<any>('/api/admin/llm/usage', {
			token: locals.token,
			query: { days: 30, group_by: 'role' },
			fetchImpl: fetch
		})
	]);
	return {
		providers: providers ?? [],
		models: models ?? [],
		roles: roles ?? [],
		usage: usage?.rows ?? []
	};
};

export const actions: Actions = {
	bind: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		const role = String(form.get('role') ?? '');
		const effort = String(form.get('reasoning_effort') ?? '');
		try {
			await api(`/api/admin/llm/roles/${role}`, {
				method: 'PUT',
				token: locals.token,
				body: {
					model_key: String(form.get('model_key') ?? ''),
					reasoning_effort: effort || null,
					temperature: Number(form.get('temperature') ?? 0.2),
					timeout_seconds: Number(form.get('timeout_seconds') ?? 120),
					concurrency: Number(form.get('concurrency') ?? 4),
					json_mode: true,
					enabled: true
				},
				fetchImpl: fetch
			});
			return { ok: true, role };
		} catch (error) {
			return fail(400, { error: (error as Error).message, role });
		}
	}
};
