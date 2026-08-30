import { api, tryApi } from '$lib/api/client';
import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, fetch }) => {
	const [settings, diff] = await Promise.all([
		tryApi<any[]>('/api/admin/settings', { token: locals.token, fetchImpl: fetch }),
		tryApi<any[]>('/api/admin/settings/env-diff', { token: locals.token, fetchImpl: fetch })
	]);
	return { settings: settings ?? [], diff: diff ?? [] };
};

export const actions: Actions = {
	set: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		const key = String(form.get('key') ?? '');
		const raw = String(form.get('value') ?? '');
		let value: unknown = raw;
		try {
			value = JSON.parse(raw);
		} catch {
			// Не JSON — значит строка. Заставлять администратора писать кавычки
			// вокруг каждого значения было бы издевательством.
		}
		try {
			await api(`/api/admin/settings/${key}`, {
				method: 'PUT',
				token: locals.token,
				body: { value },
				fetchImpl: fetch
			});
			return { ok: true, key };
		} catch (error) {
			return fail(400, { error: (error as Error).message, key });
		}
	},
	reset: async ({ request, locals, fetch }) => {
		const key = String((await request.formData()).get('key') ?? '');
		try {
			await api(`/api/admin/settings/${key}`, {
				method: 'DELETE',
				token: locals.token,
				fetchImpl: fetch
			});
			return { ok: true, key };
		} catch (error) {
			return fail(400, { error: (error as Error).message, key });
		}
	}
};
