/**
 * Тонкая обёртка над FastAPI.
 *
 * Токен живёт в httpOnly-cookie и в браузер не попадает: все запросы делает
 * node-слой SvelteKit по внутреннему адресу. Поэтому здесь нет ни хранилища
 * токенов, ни интерцепторов на клиенте — только заголовок и разбор ошибки.
 */

import { env } from '$env/dynamic/private';

export const API_BASE = env.API_INTERNAL_URL ?? 'http://127.0.0.1:8000';

export class ApiError extends Error {
	constructor(
		readonly status: number,
		message: string,
		readonly body?: unknown
	) {
		super(message);
	}
}

type Options = {
	token?: string | null;
	method?: string;
	body?: unknown;
	query?: Record<string, string | number | boolean | undefined | null>;
	fetchImpl?: typeof fetch;
};

function url(path: string, query?: Options['query']): string {
	const target = new URL(path.startsWith('/') ? path : `/${path}`, API_BASE);
	for (const [key, value] of Object.entries(query ?? {})) {
		if (value !== undefined && value !== null && value !== '') {
			target.searchParams.set(key, String(value));
		}
	}
	return target.toString();
}

export async function api<T = unknown>(path: string, options: Options = {}): Promise<T> {
	const doFetch = options.fetchImpl ?? fetch;
	const headers: Record<string, string> = { Accept: 'application/json' };
	if (options.token) headers.Authorization = `Bearer ${options.token}`;
	if (options.body !== undefined) headers['Content-Type'] = 'application/json';

	const response = await doFetch(url(path, options.query), {
		method: options.method ?? 'GET',
		headers,
		body: options.body === undefined ? undefined : JSON.stringify(options.body)
	});

	if (response.status === 204) return undefined as T;

	const text = await response.text();
	const parsed = text ? safeJson(text) : undefined;
	if (!response.ok) {
		// detail от FastAPI — это то, что стоит показать администратору;
		// «Ошибка 500» не помогает никому.
		const detail =
			(parsed as { detail?: unknown })?.detail ?? response.statusText ?? 'Ошибка запроса';
		throw new ApiError(
			response.status,
			typeof detail === 'string' ? detail : JSON.stringify(detail),
			parsed
		);
	}
	return parsed as T;
}

function safeJson(text: string): unknown {
	try {
		return JSON.parse(text);
	} catch {
		return text;
	}
}

/** Запрос, который не должен ронять страницу: экран собирается из многих панелей. */
export async function tryApi<T>(path: string, options: Options = {}): Promise<T | null> {
	try {
		return await api<T>(path, options);
	} catch {
		return null;
	}
}
