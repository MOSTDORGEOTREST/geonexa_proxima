<script lang="ts">
	/**
	 * Постраничная навигация, сохраняющая остальные параметры адреса.
	 *
	 * Ссылки, а не кнопки с обработчиками: страницу можно открыть в новой
	 * вкладке, переслать и вернуться к ней по истории браузера.
	 */
	import { page } from '$app/stores';

	let {
		current,
		pages,
		total,
		perPage
	}: { current: number; pages: number; total: number; perPage: number } = $props();

	const href = (target: number): string => {
		const params = new URLSearchParams($page.url.searchParams);
		if (target <= 1) params.delete('page');
		else params.set('page', String(target));
		const query = params.toString();
		return `${$page.url.pathname}${query ? `?${query}` : ''}`;
	};

	const first = $derived(total ? (current - 1) * perPage + 1 : 0);
	const last = $derived(Math.min(current * perPage, total));

	/** Соседи текущей страницы плюс края — без простыни из сотни номеров. */
	const numbers = $derived.by(() => {
		const set = new Set<number>([1, pages, current - 1, current, current + 1]);
		return [...set].filter((value) => value >= 1 && value <= pages).sort((a, b) => a - b);
	});
</script>

{#if pages > 1 || total > 0}
	<nav class="pager" aria-label="Страницы">
		<span class="muted small">{first}–{last} из {total}</span>
		{#if pages > 1}
			<div class="links">
				<a class="btn" class:disabled={current <= 1} href={href(current - 1)} aria-label="Назад">←</a>
				{#each numbers as number, index}
					{#if index > 0 && number - numbers[index - 1] > 1}
						<span class="muted">…</span>
					{/if}
					<a class="btn" class:active={number === current} href={href(number)}>{number}</a>
				{/each}
				<a
					class="btn"
					class:disabled={current >= pages}
					href={href(current + 1)}
					aria-label="Вперёд">→</a
				>
			</div>
		{/if}
	</nav>
{/if}

<style>
	.pager {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--gap);
		flex-wrap: wrap;
		padding: 12px 18px;
		border-top: 1px solid var(--border-soft);
	}

	.links {
		display: flex;
		gap: 6px;
		align-items: center;
	}

	.links .btn {
		padding: 4px 12px;
		font-size: 12.5px;
		text-decoration: none;
	}

	.links .active {
		border-color: var(--accent);
		color: var(--accent);
	}

	.links .disabled {
		opacity: 0.4;
		pointer-events: none;
	}

	.small {
		font-size: 12.5px;
	}
</style>
