import { api, tryApi } from '$lib/api/client';
import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, url, fetch }) => {
	const list = await tryApi<any>('/api/admin/chats', {
		token: locals.token,
		query: {
			page: url.searchParams.get('page') ?? 1,
			present_only: url.searchParams.get('present') === '1' ? true : undefined,
			q: url.searchParams.get('q') ?? undefined
		},
		fetchImpl: fetch
	});
	return { list, present: url.searchParams.get('present') === '1' };
};

export const actions: Actions = {
	refresh: async ({ request, locals, fetch }) => {
		const id = String((await request.formData()).get('id') ?? '');
		try {
			const result = await api<any>(`/api/admin/chats/${id}/refresh`, {
				method: 'POST',
				token: locals.token,
				fetchImpl: fetch
			});
			return { ok: true, refreshed: result };
		} catch (error) {
			return fail(502, { error: (error as Error).message });
		}
	},
	approve: async ({ request, locals, fetch }) => {
		const id = String((await request.formData()).get('id') ?? '');
		try {
			const result = await api<any>(`/api/admin/subscribers/${id}/approve`, {
				method: 'POST',
				token: locals.token,
				body: { notify: true, grant_trial: true },
				fetchImpl: fetch
			});
			// Отдаём отчёт как есть: подтверждение может пройти, а приветствие —
			// нет (бота уже выгнали), и говорить «отправлено» в этом случае значит
			// закрыть администратору единственный повод разобраться.
			return { ok: true, approval: result?.approval ?? null };
		} catch (error) {
			return fail(400, { error: (error as Error).message });
		}
	},
	test: async ({ request, locals, fetch }) => {
		const id = String((await request.formData()).get('id') ?? '');
		try {
			await api(`/api/admin/chats/${id}/test-message`, {
				method: 'POST',
				token: locals.token,
				body: { text: 'Проксима на связи. Это проверочное сообщение.' },
				fetchImpl: fetch
			});
			return { ok: true, sent: true };
		} catch (error) {
			return fail(502, { error: (error as Error).message });
		}
	}
};
