import { ApiError, api, tryApi } from '$lib/api/client';
import { error, fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, fetch, params }) => {
	// Карточку берём через `api`, а не `tryApi`: `tryApi` гасит и 404, и 503, и
	// таймаут одинаково, и падение базы объявлялось бы «подписчик не найден» —
	// самая обидная из диагностик, потому что она уводит от причины.
	let card: any;
	try {
		card = await api<any>(`/api/admin/subscribers/${params.id}`, {
			token: locals.token,
			fetchImpl: fetch
		});
	} catch (err) {
		const status = (err as ApiError).status ?? 502;
		throw error(status === 404 ? 404 : 502, (err as Error).message);
	}
	// Интересы читаются у активного профиля: остальные профили видны списком,
	// но правится тот, по которому реально собирается дайджест.
	const active = (card.profiles ?? []).find((row: any) => row.is_active) ?? card.profiles?.[0];
	const [interests, review, activity] = await Promise.all([
		active
			? tryApi<any>(`/api/admin/profiles/${active.id}/interests`, {
					token: locals.token,
					fetchImpl: fetch
				})
			: Promise.resolve(null),
		// Разбор сохранённого профиля: на какие темы он распался и что в нём не
		// сработает. Ошибка в профиле не падает — она молча портит выдачу, и
		// увидеть её можно только так.
		active
			? tryApi<any>(`/api/admin/profiles/${active.id}/preview`, {
					token: locals.token,
					fetchImpl: fetch
				})
			: Promise.resolve(null),
		tryApi<any>(`/api/admin/subscribers/${params.id}/activity`, {
			token: locals.token,
			query: { per_page: 20 },
			fetchImpl: fetch
		})
	]);
	return { card, active: active ?? null, interests, review, activity };
};

/**
 * Число из поля формы — или null, если поле пустое.
 *
 * `Number('')` — это ноль, а не «не заполнено». Очищенный «Порог» уезжал бы на
 * сервер как `min_personal_score = 0`, то есть «пропускать вообще всё», и
 * администратор, стерший значение ради значения по умолчанию, получал бы
 * противоположное.
 */
function numeric(value: FormDataEntryValue | null): number | null {
	const text = String(value ?? '').trim();
	if (!text) return null;
	const parsed = Number(text);
	return Number.isFinite(parsed) ? parsed : null;
}

async function call(
	path: string,
	locals: App.Locals,
	fetchImpl: typeof fetch,
	body?: unknown,
	method = 'POST'
) {
	try {
		await api(path, { method, token: locals.token, body, fetchImpl });
		return { ok: true };
	} catch (err) {
		return fail(400, { error: (err as Error).message });
	}
}

export const actions: Actions = {
	approve: async ({ locals, fetch, params }) =>
		call(`/api/admin/subscribers/${params.id}/approve`, locals, fetch, {
			notify: true,
			grant_trial: true
		}),

	block: async ({ locals, fetch, params }) =>
		call(`/api/admin/subscribers/${params.id}/block`, locals, fetch),

	profile: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		const id = String(form.get('profile_id') ?? '');
		const body: Record<string, unknown> = {
			// Пустая строка, а не null: null означает «не трогать», и очистить
			// описание было бы нечем — форма молча не сохраняла бы пустое поле.
			description: String(form.get('description') ?? '').trim(),
			digest_enabled: form.get('digest_enabled') === 'on'
		};
		const maxItems = numeric(form.get('max_items'));
		if (maxItems !== null && maxItems > 0) body.max_items = maxItems;
		const score = numeric(form.get('min_personal_score'));
		if (score !== null) body.min_personal_score = score;
		const format = String(form.get('delivery_format') ?? '');
		if (format) body.delivery_format = format;
		return call(`/api/admin/profiles/${id}`, locals, fetch, body, 'PATCH');
	},

	interest: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		const id = String(form.get('profile_id') ?? '');
		const query = String(form.get('query') ?? '').trim();
		if (!query) return fail(400, { error: 'Тема не может быть пустой' });
		return call(`/api/admin/profiles/${id}/interests`, locals, fetch, {
			query,
			polarity: String(form.get('polarity') ?? 'positive'),
			weight: numeric(form.get('weight')) ?? 5
		});
	},

	drop_interest: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		const id = String(form.get('profile_id') ?? '');
		const interest = String(form.get('interest_id') ?? '');
		return call(
			`/api/admin/profiles/${id}/interests/${interest}`,
			locals,
			fetch,
			undefined,
			'DELETE'
		);
	},

	message: async ({ request, locals, fetch, params }) => {
		const text = String((await request.formData()).get('text') ?? '').trim();
		if (!text) return fail(400, { error: 'Пустое сообщение' });
		return call(`/api/admin/subscribers/${params.id}/message`, locals, fetch, { text });
	}
};
