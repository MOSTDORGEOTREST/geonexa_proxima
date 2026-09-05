import { api, tryApi } from '$lib/api/client';
import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, fetch, url }) => {
	const kind = url.searchParams.get('kind') ?? '';
	const state = url.searchParams.get('state') ?? '';
	const [runs, flows, schedules, health] = await Promise.all([
		tryApi<any[]>('/api/admin/prefect/flow-runs', {
			token: locals.token,
			query: { limit: 60, ...(kind ? { kind } : {}), ...(state ? { state } : {}) },
			fetchImpl: fetch
		}),
		tryApi<any[]>('/api/admin/schedules/flows', { token: locals.token, fetchImpl: fetch }),
		tryApi<any[]>('/api/admin/schedules', { token: locals.token, fetchImpl: fetch }),
		tryApi<any>('/api/admin/prefect/health', { token: locals.token, fetchImpl: fetch })
	]);
	return {
		runs: runs ?? [],
		flows: flows ?? [],
		schedules,
		health,
		filters: { kind, state }
	};
};

/** Собрать параметры флоу из полей формы.
 *
 * Поля с префиксом `p.` — форма по описанию флоу, `parameters_json` —
 * свободный JSON для того, чего форма не знает. JSON сильнее полей: он
 * существует ради ручного переопределения. Типы приводит API: здесь всё
 * остаётся строками, чтобы правила разбора жили в одном месте.
 */
function collectParameters(form: FormData): { parameters: Record<string, unknown>; error?: string } {
	const parameters: Record<string, unknown> = {};
	const booleans = new Set<string>();
	for (const [name, raw] of form.entries()) {
		if (typeof raw !== 'string') continue;
		if (name.startsWith('p.__bool.')) {
			booleans.add(name.slice('p.__bool.'.length));
			continue;
		}
		if (name.startsWith('p.')) parameters[name.slice(2)] = raw;
	}
	// Снятый флажок в форме отсутствует — без маркера его нельзя отличить от
	// «не трогали», и выключить `deliver` было бы невозможно.
	for (const key of booleans) if (!(key in parameters)) parameters[key] = 'false';
	const json = String(form.get('parameters_json') ?? '').trim();
	if (json) {
		try {
			const extra = JSON.parse(json);
			if (!extra || typeof extra !== 'object' || Array.isArray(extra)) {
				return { parameters, error: 'JSON параметров должен быть объектом {…}' };
			}
			Object.assign(parameters, extra);
		} catch (error) {
			return { parameters, error: `JSON параметров не разбирается: ${(error as Error).message}` };
		}
	}
	return { parameters };
}

const UNIT_SECONDS: Record<string, number> = { minutes: 60, hours: 3600, days: 86400 };

export const actions: Actions = {
	/** Сохранить период, параметры и включённость одного расписания. */
	save: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		const id = String(form.get('id') ?? '');
		if (!id) return fail(400, { error: 'Не передан id расписания' });
		const body: Record<string, unknown> = {};
		const mode = String(form.get('mode') ?? 'cron');
		if (mode === 'interval') {
			const value = Number(form.get('interval_value') ?? 0);
			const unit = String(form.get('interval_unit') ?? 'hours');
			const seconds = Math.round(value * (UNIT_SECONDS[unit] ?? 3600));
			if (!Number.isFinite(seconds) || seconds < 60) {
				return fail(400, { error: 'Интервал должен быть не меньше минуты', id });
			}
			body.interval_seconds = seconds;
		} else {
			const cron = String(form.get('cron') ?? '').trim();
			if (!cron) return fail(400, { error: 'Cron-выражение пустое', id });
			body.cron = cron;
		}
		const timezone = String(form.get('timezone') ?? '').trim();
		if (timezone) body.timezone = timezone;
		body.enabled = form.get('enabled') === 'on';
		const { parameters, error } = collectParameters(form);
		if (error) return fail(400, { error, id });
		body.parameters = parameters;
		try {
			const result = await api<any>(`/api/admin/schedules/${id}`, {
				method: 'PATCH',
				token: locals.token,
				body,
				fetchImpl: fetch
			});
			return { saved: id, synced: result?.synced ?? null, reason: result?.reason ?? null };
		} catch (err) {
			return fail(400, { error: (err as Error).message, id });
		}
	},

	toggle: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		const id = String(form.get('id') ?? '');
		try {
			await api(`/api/admin/schedules/${id}/toggle`, {
				method: 'POST',
				token: locals.token,
				fetchImpl: fetch
			});
			return { toggled: id };
		} catch (err) {
			return fail(400, { error: (err as Error).message, id });
		}
	},

	/** Запустить флоу сейчас — с параметрами расписания, кнопки или формы. */
	run: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		const id = String(form.get('id') ?? '');
		const label = String(form.get('label') ?? 'Запуск');
		if (!id) return fail(409, { error: 'Расписание этого флоу отсутствует в базе' });
		const { parameters, error } = collectParameters(form);
		if (error) return fail(400, { error, id });
		// Параметры кнопки-пресета (JSON в скрытом поле) дописываются поверх.
		const preset = String(form.get('preset') ?? '').trim();
		if (preset) {
			try {
				Object.assign(parameters, JSON.parse(preset));
			} catch {
				return fail(400, { error: 'Параметры кнопки повреждены', id });
			}
		}
		try {
			const result = await api<any>(`/api/admin/schedules/${id}/run`, {
				method: 'POST',
				token: locals.token,
				body: { parameters, merge: form.get('merge') !== 'off' },
				fetchImpl: fetch
			});
			return { started: result?.flow_run_id ?? true, label, id };
		} catch (err) {
			return fail(503, { error: (err as Error).message, id });
		}
	},

	resync: async ({ locals, fetch }) => {
		try {
			const result = await api<any>('/api/admin/prefect/resync', {
				method: 'POST',
				token: locals.token,
				fetchImpl: fetch
			});
			return { resynced: result };
		} catch (err) {
			return fail(503, { error: (err as Error).message });
		}
	}
};
