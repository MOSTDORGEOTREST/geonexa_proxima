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

/** Число из формы. Пустое поле — это «не задано», а не ноль. */
function numeric(value: FormDataEntryValue | null, fallback: number | null): number | null {
	const raw = String(value ?? '').trim();
	if (!raw) return fallback;
	const parsed = Number(raw);
	return Number.isFinite(parsed) ? parsed : fallback;
}

/** Строка из формы. Пустая — это `null`, а не пустая строка. */
function text(value: FormDataEntryValue | null): string | null {
	const raw = String(value ?? '').trim();
	return raw || null;
}

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
					// Пустое поле — это не ноль: `Number('')` даёт 0, и очищенная
					// температура молча становилась нулевой вместо умолчания.
					temperature: numeric(form.get('temperature'), 0.2),
					fallback_model_key: text(form.get('fallback_model_key')),
					max_tokens: numeric(form.get('max_tokens'), null),
					system_prompt_override: text(form.get('system_prompt_override')),
					timeout_seconds: numeric(form.get('timeout_seconds'), 120),
					concurrency: numeric(form.get('concurrency'), 4),
					json_mode: Boolean(form.get('json_mode')),
					enabled: Boolean(form.get('enabled'))
				},
				fetchImpl: fetch
			});
			return { ok: true, role };
		} catch (error) {
			return fail(400, { error: (error as Error).message, role });
		}
	}
};
