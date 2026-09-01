<script lang="ts">
	import { page } from '$app/stores';

	const status = $derived($page.status);
	const message = $derived($page.error?.message ?? 'Что-то пошло не так');
	const known: Record<number, string> = {
		404: 'Такой страницы нет — или запись уже удалена.',
		502: 'API не отвечает. Проверьте контейнер api и подключение к базе.',
		503: 'Сервис временно недоступен: часть зависимостей не поднялась.'
	};
</script>

<svelte:head><title>{status} · Проксима</title></svelte:head>

<section class="panel">
	<header><h2>{status}</h2></header>
	<div class="body">
		<p>{message}</p>
		{#if known[status]}<p class="muted">{known[status]}</p>{/if}
		<div class="row">
			<a class="btn" href="/">На дашборд</a>
			<a class="btn" href="/runs">К запускам</a>
		</div>
	</div>
</section>

<style>
	p {
		margin: 0;
		max-width: 78ch;
	}
</style>
