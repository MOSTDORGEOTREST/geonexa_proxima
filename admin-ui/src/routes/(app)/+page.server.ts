import { tryApi } from '$lib/api/client';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, fetch, url }) => {
	const days = Number(url.searchParams.get('days') ?? 30);
	const token = locals.token;
	// Панели независимы: недоступный Prefect не должен прятать состояние базы.
	const [summary, funnel, timeline] = await Promise.all([
		tryApi<Record<string, any>>('/api/admin/dashboard/summary', { token, fetchImpl: fetch }),
		tryApi<Record<string, any>>('/api/admin/dashboard/funnel', {
			token,
			query: { days },
			fetchImpl: fetch
		}),
		tryApi<Record<string, any>>('/api/admin/dashboard/timeline', {
			token,
			query: { days },
			fetchImpl: fetch
		})
	]);
	return { summary, funnel, timeline, days };
};
