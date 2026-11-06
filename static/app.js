const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = { products: [], preferences: null, detailId: null, monitoring: null };
const money = value => value == null ? '—' : (value / 100).toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' });
const date = value => new Date(value).toLocaleDateString('pt-BR');
const dateTime = value => new Date(value).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' });
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const productById = id => state.products.find(p => p.id === Number(id));
let pollTimer;
const icon = `<svg class="placeholder-icon" viewBox="0 0 100 100" fill="none" aria-hidden="true"><rect x="18" y="24" width="64" height="49" rx="6" stroke="currentColor" stroke-width="2"/><path d="M19 37h62M40 24v13m20-13v13M38 82h24M50 73v9" stroke="currentColor" stroke-width="2"/><path d="m38 53 9 8 16-16" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'X-Requested-With': 'PrecoCerto', ...(options.body && !(options.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Não foi possível concluir. Tente novamente.');
  return data;
}
let toastTimer;
function toast(message) {
  $('#toast').textContent = message;
  $('#toast').classList.add('toast-visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('#toast').classList.remove('toast-visible'), 5000);
}
function run(action) { return Promise.resolve().then(action).catch(error => toast(error.message)); }
async function load() {
  const previous = state.products.find(p => p.id === state.detailId)?.collection;
  const [products, monitoring] = await Promise.all([api('/api/products'),api('/api/monitoring')]);
  state.products=products;
  state.monitoring=monitoring;
  renderMonitoring();
  render();
  clearTimeout(pollTimer);
  if (state.products.some(p => ['queued', 'running'].includes(p.collection?.status))) {
    pollTimer = setTimeout(() => load().catch(error => toast(error.message)), 2500);
  }
  const current = productById(state.detailId)?.collection;
  if ($('#detail-dialog').open && previous?.status !== current?.status) await openDetail(state.detailId);
}
function render() {
  const active = state.products.filter(p => !p.archived);
  $('#nav-count').textContent = active.length;
  $('#metric-products').textContent = active.length;
  $('#metric-searches').textContent = state.products.reduce((sum, p) => sum + p.history.length, 0);
  $('#metric-target').textContent = active.filter(p => p.history.length && p.history.at(-1).best_total_cents != null && p.history.at(-1).best_total_cents <= p.target_cents).length;
  const query = $('#filter').value.toLocaleLowerCase('pt-BR');
  const mode = $('#status-filter').value;
  const products = state.products.filter(p => (mode === 'all' || p.archived === (mode === 'archived')) && `${p.name} ${p.brand} ${p.model}`.toLocaleLowerCase('pt-BR').includes(query));
  $('#list-count').textContent = products.length;
  if (!products.length) {
    const filtered = query || mode !== 'active';
    $('#products').innerHTML = `<div class="empty-state"><div class="empty-icon">↘</div><h3>${filtered ? 'Nenhum produto por aqui' : 'Sua próxima boa compra começa aqui'}</h3><p>${filtered ? 'Tente outro termo ou altere o filtro para encontrar seus produtos.' : 'Cadastre um produto, defina quanto quer pagar e comece a construir seu histórico de preços.'}</p>${filtered ? '' : '<button class="button primary" data-action="new">＋ Cadastrar meu primeiro produto</button>'}</div>`;
    return;
  }
  $('#products').innerHTML = products.map(p => {
    const latest = p.history.at(-1);
    const total=p.best_offer?.confirmed_final_cents;
    const best = total ?? p.best_offer?.amount_cents ?? p.price_summary.current_cents;
    const reached = total != null && total <= p.target_cents;
    const status = p.archived ? 'Produto arquivado' : !latest ? 'Aguardando a primeira pesquisa' : total == null ? 'Total pendente: frete ou condições ainda não confirmados.' : reached ? '✓ Total com frete dentro do seu objetivo' : `${money(total - p.target_cents)} acima do objetivo, com frete`;
    return `<article class="product-card"><div class="product-image" data-action="detail" data-id="${p.id}" role="button" tabindex="0" aria-label="Ver histórico de ${esc(p.name)}"><span class="category">${esc(p.category)}</span>${p.photos.length ? `<img src="/photos/${esc(p.photos[0].filename)}" alt="${esc(p.name)}" loading="lazy">` : icon}</div><div class="product-body"><div class="product-brand">${esc(p.brand)}</div><h3 class="product-name">${esc(p.name)}</h3><p class="product-model">${esc(p.model)} · ${esc(p.desired_condition)}${p.desired_size ? ` · Tam. ${esc(p.desired_size)}` : ''}${p.preferred_colors ? ` · ${esc(p.preferred_colors)}` : ''}</p><div class="price-row"><div><small>${total == null ? 'Preço do produto · total pendente' : 'Menor total confirmado · com frete'}</small><strong>${money(best)}</strong></div><div class="target"><small>Quero pagar · com frete</small><strong>${money(p.target_cents)}</strong></div></div><div class="status-line ${reached ? 'good' : 'waiting'}">${esc(status)}</div><p class="price-comparison">Histórico sem frete: ${esc(comparisonText(p.price_summary))}</p>${coverageNotice(p)}${bestOfferLink(p)}${collectionBlock(p)}<div class="card-edit-action"><button class="button" data-action="edit" data-id="${p.id}">Editar produto</button></div><div class="card-footer"><span>${latest ? `Verificado em ${date(latest.verified_at)}` : 'Seu histórico começa na 1ª oferta'}</span><button data-action="detail" data-id="${p.id}">Ver histórico ↗</button></div></div></article>`;
  }).join('');
}

function comparisonText(summary) {
  if (!summary || summary.verdict === 'unavailable') return 'Ainda sem preços registrados.';
  if (summary.verdict === 'first') return 'Primeiro registro · ainda sem histórico anterior para comparar.';
  if (summary.verdict === 'equal') return 'Igual ao menor preço registrado anteriormente.';
  const percent = summary.difference_percent == null ? '' : ` (${Math.abs(summary.difference_percent).toLocaleString('pt-BR')}%)`;
  return `${money(Math.abs(summary.difference_cents))}${percent} ${summary.verdict === 'lower' ? 'abaixo' : 'acima'} do menor preço anterior de ${money(summary.previous_low_cents)}.`;
}
function collectionBlock(product, detailed = false) {
  const job = product.collection;
  const pending = ['queued', 'running'].includes(job?.status);
  const reports = detailed && job?.report?.length ? `<ul>${job.report.map(r => `<li><strong>${esc(r.store)}</strong>: ${esc(r.message)}${r.count ? ` (${r.count} ofertas)` : ''}</li>`).join('')}</ul>` : '';
  return `<div class="collection-state ${pending ? 'pending' : ''}" role="status"><span>${esc(job?.message || 'Consulte preços nas lojas públicas integradas.')}</span>${job?.finished_at ? `<small>Consulta: ${esc(dateTime(job.finished_at))}</small>` : ''}${reports}${!product.archived && !detailed ? `<button class="text-button" data-action="collect" data-id="${product.id}" ${pending ? 'disabled' : ''}>${pending ? 'Pesquisando…' : '⌕ Pesquisar agora'}</button>` : ''}</div>`;
}


function bestOfferLink(product) {
  const offer = product.best_offer;
  if (!offer) return '';
  return `<div class="best-purchase"><span>${esc(offer.store)}</span><small>${esc(offer.title || product.name)}</small><a class="button primary" href="${esc(offer.url)}" target="_blank" rel="noopener noreferrer">Ver produto na loja ↗</a>${offer.payment_known === false ? '<small>Forma de pagamento a confirmar na loja.</small>' : ''}</div>`;
}
function purchaseOptions(product, offers) {
  const latest = product.history.at(-1);
  if (!latest) return '';
  const sorted = offers.filter(o => o.search_id === latest.id).sort((a, b) => (a.price_cents - a.discount_cents) - (b.price_cents - b.discount_cents));
  const stores = new Map();
  for (const offer of sorted) {
    const key = `${new URL(offer.url).hostname}|${offer.store}`;
    if (!stores.has(key)) stores.set(key, offer);
  }
  return `<section class="purchase-section"><h3 class="details-subtitle">Onde comprar</h3><p class="helper">Menor preço da última pesquisa por loja, sem frete. Confira a variação, o pagamento e o estoque antes de comprar.</p><div class="purchase-grid">${[...stores.values()].map(o => `<article class="purchase-option"><strong>${esc(o.store)}</strong><p>${esc(o.title || product.name)}</p><b>${money(o.price_cents - o.discount_cents)}</b><small>${esc(o.payment)} · Frete ${o.shipping_known ? money(o.shipping_cents) : 'a calcular'}</small><a class="button primary" href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">Ver produto na loja ↗</a></article>`).join('')}</div></section>`;
}
function discoveredLinks(product) {
  const links = (product.collection?.report || []).flatMap(report => report.links || []).filter(link => link.status !== 'verified' && /^https:\/\//.test(link.url));
  const query = encodeURIComponent(product.search_query || `${product.brand} ${product.model} ${product.requirements}`);
  return `<section class="purchase-section"><h3 class="details-subtitle">Mais lojas para conferir</h3><p class="helper">Páginas encontradas na busca pública. Preço e disponibilidade não confirmados; estes links ficam fora do histórico de preços.</p>${links.length ? `<div class="discovered-links">${links.map(link => `<article><strong>${esc(link.store)}</strong><p>${esc(link.title)}</p><small>${esc(link.message)}</small><a class="button" href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">Conferir produto na loja ↗</a></article>`).join('')}</div>` : '<p class="helper">Nenhuma página adicional foi confirmada nesta consulta.</p>'}<a class="button" href="https://www.google.com/search?tbm=shop&amp;q=${query}" target="_blank" rel="noopener noreferrer">Ampliar no Google Shopping ↗</a></section>`;
}

function openProduct(id) {
  const form = $('#product-form');
  form.reset();
  $('.form-error', form).textContent = '';
  const product = id ? productById(id) : null;
  $('#product-title').textContent = product ? 'Editar produto' : 'Cadastrar produto';
  form.elements.id.value = product?.id || '';
  if (product) {
    for (const key of ['name', 'brand', 'model', 'category', 'requirements', 'desired_condition', 'desired_size', 'preferred_colors']) form.elements[key].value = product[key] || '';
    form.elements.target.value = (product.target_cents / 100).toFixed(2);
  }
  $('button[type=submit]', form).textContent = product?.archived ? 'Salvar alterações' : state.monitoring?.phase === 'paused' ? 'Salvar e programar' : 'Salvar e pesquisar';
  updateProductQuery();
  $('#product-delivery-context').textContent = 'Conferindo preferências de entrega…';
  api('/api/preferences').then(preferences => {
    $('#product-delivery-context').textContent = preferences.cep
      ? 'CEP de entrega definido. O frete será consultado nas lojas compatíveis; se não for confirmado, o total ficará pendente.'
      : 'CEP de entrega não preenchido. Você pode defini-lo em Preferências.';
  }).catch(() => { $('#product-delivery-context').textContent = 'Confira o CEP em Preferências.'; });
  $('#product-dialog').showModal();
}
function chart(product) {
  const history = product.history;
  if (!history.length) return '<div class="empty-state"><h3>O primeiro ponto é seu</h3><p>A pesquisa automática começa ao cadastrar. Você também pode pesquisar agora ou registrar uma oferta manualmente.</p></div>';
  const w = 800, h = 220, left = 88, right = 20, top = 20, bottom = 38;
  const values = history.map(p => p.best_price_cents);
  const min = Math.max(0, Math.min(...values) * .9);
  const max = Math.max(...values) * 1.1 || 100;
  const first = +new Date(history[0].verified_at), last = +new Date(history.at(-1).verified_at);
  const x = point => first === last ? (left + w - right) / 2 : left + (+new Date(point.verified_at) - first) / (last - first) * (w - left - right);
  const y = value => top + (max - value) / (max - min) * (h - top - bottom);
  const line = history.map(p => `${x(p)},${y(p.best_price_cents)}`).join(' ');
  let grid = '';
  for (let i = 0; i < 4; i++) {
    const value = min + (max - min) * i / 3;
    grid += `<line class="chart-grid" x1="${left}" y1="${y(value)}" x2="${w - right}" y2="${y(value)}"/><text class="chart-label" x="${left - 12}" y="${y(value) + 4}" text-anchor="end">${esc(money(Math.round(value)))}</text>`;
  }
  const labels = history.length <= 4 ? history : [history[0], history[Math.floor(history.length / 2)], history.at(-1)];
  return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Menor preço sem frete de cada pesquisa, em ordem cronológica. Os valores também estão na tabela de ofertas.">${grid}<polygon class="chart-fill" points="${x(history[0])},${h - bottom} ${line} ${x(history.at(-1))},${h - bottom}"/><polyline class="chart-line" points="${line}"/>${history.map(p => `<circle class="chart-dot" cx="${x(p)}" cy="${y(p.best_price_cents)}" r="5"><title>${esc(dateTime(p.verified_at))}: ${esc(money(p.best_price_cents))}</title></circle>`).join('')}${labels.map(p => `<text class="chart-label" x="${x(p)}" y="${h - 12}" text-anchor="middle">${date(p.verified_at)}</text>`).join('')}</svg>`;
}
async function openDetail(id) {
  const p = productById(id);
  if (!p) return;
  const offers = await api(`/api/products/${p.id}/offers`);
  state.detailId = p.id;
  const latest = p.history.at(-1);
  const best = p.price_summary.historical_low_cents;
  const difference = p.price_summary.difference_cents;
  const diffText = difference == null ? '—' : `${difference > 0 ? '+' : difference < 0 ? '−' : ''}${money(Math.abs(difference))}`;
  $('#detail-content').innerHTML = `<div class="dialog-heading"><div><div class="eyebrow">${esc(p.brand)} · ${esc(p.category)}</div><h2>${esc(p.name)}</h2><p>${esc(p.model)} · ${esc(p.desired_condition)}${p.desired_size ? ` · Tam. ${esc(p.desired_size)}` : ''}${p.preferred_colors ? ` · ${esc(p.preferred_colors)}` : ''}${p.archived ? ' · Arquivado' : ''}</p></div><button class="icon-button close" aria-label="Fechar">×</button></div><div class="detail-summary"><div class="gallery">${p.photos.length ? p.photos.map(photo => `<a href="/photos/${esc(photo.filename)}" target="_blank" rel="noopener"><img src="/photos/${esc(photo.filename)}" alt="Foto de ${esc(p.name)}"></a>`).join('') : icon}</div><div class="detail-info"><div class="eyebrow">CARACTERÍSTICAS OBRIGATÓRIAS</div><p>${esc(p.requirements || 'Nenhuma característica adicional cadastrada.')}</p><p>Tamanho: <strong>${esc(p.desired_size || 'Qualquer')}</strong><br>Cores: <strong>${esc(p.preferred_colors || 'Qualquer')}</strong></p><p>Meta de total com frete: <strong>${money(p.target_cents)}</strong></p><div class="detail-actions">${p.archived ? '' : `<button class="button primary" data-action="collect" data-id="${p.id}">⌕ Pesquisar agora</button><button class="button" data-action="research" data-id="${p.id}">＋ Registrar manualmente</button>`}<button class="button" data-action="edit" data-id="${p.id}">Editar produto</button><button class="button" data-action="archive" data-id="${p.id}">${p.archived ? 'Reativar' : 'Arquivar'}</button></div></div></div>${collectionBlock(p, true)}${coverageNotice(p)}${shoppingResults(p)}<p class="comparison-caption">${esc(comparisonText(p.price_summary))} · Comparação de preços sem frete.</p><div class="detail-prices"><div><small>MENOR PREÇO REGISTRADO</small><strong>${money(best)}</strong></div><div><small>PREÇO DA ÚLTIMA PESQUISA</small><strong>${money(latest?.best_price_cents)}</strong></div><div><small>VS. MENOR PREÇO ANTERIOR</small><strong>${diffText}</strong></div></div><section class="chart-box"><h3>A história do preço</h3><p>Histórico do preço do produto, sem frete. Sua meta de compra considera o total entregue.</p>${chart(p)}</section>${purchaseOptions(p, offers)}${discoveredLinks(p)}<h3 class="details-subtitle">Todas as ofertas <span class="subtext">(${offers.length})</span></h3><div class="table-scroll">${offers.length ? `<table><thead><tr><th>Verificado em</th><th>Loja / condição</th><th>Preço</th><th>Frete</th><th>Desconto</th><th>Total</th><th>Pagamento</th><th>Acessar produto</th></tr></thead><tbody>${offers.map(o => `<tr><td>${esc(dateTime(o.verified_at))}<div class="subtext">Pesquisa #${o.search_id} · ${o.source === 'catalogo_publico' ? 'automática' : 'manual'}</div></td><td><a class="store-link" href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">${esc(o.store)} ↗</a>${o.title ? `<div class="offer-title">${esc(o.title)}</div>` : ''}<span class="subtext">${esc(o.condition)}${o.product_revision !== p.revision ? ' · Configuração anterior' : ''}${!o.imported_known ? ' · Origem não informada' : o.imported ? ' · Importado' : ''}</span></td><td>${money(o.price_cents)}</td><td>${o.shipping_known ? money(o.shipping_cents) : 'A calcular'}</td><td>${money(o.discount_cents)}</td><td><strong>${o.final_cents == null ? 'A calcular' : money(o.final_cents)}</strong></td><td>${esc(o.payment)}</td><td><a class="button shop-link" href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">Ver na loja ↗</a></td></tr>`).join('')}</tbody></table>` : '<p class="helper">Você ainda não registrou nenhuma oferta para este produto.</p>'}</div>${[...p.history].reverse().filter(s => s.notes).map(s => `<div class="search-notes"><strong>Pesquisa #${s.id} · ${esc(dateTime(s.verified_at))}</strong><br>${esc(s.notes)}</div>`).join('')}<p class="helper">O histórico registra o que foi verificado em cada data. Os preços das lojas podem ter mudado desde então. As pesquisas anteriores são preservadas.</p>`;
  if (!$('#detail-dialog').open) $('#detail-dialog').showModal();
}
function localNow() {
  const now = new Date();
  return new Date(now - now.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
function addOffer() {
  const number = $$('.offer-row').length + 1;
  const section = document.createElement('section');
  section.className = 'offer-row';
  section.innerHTML = `<div class="offer-heading"><h3>Oferta <span class="offer-number">${number}</span></h3><button type="button" data-action="remove-offer">Remover</button></div><div class="form-grid"><label>Loja<input name="store" required maxlength="160" placeholder="Nome da loja"></label><label>Link da oferta<input name="url" type="url" required maxlength="2000" placeholder="https://..."></label><div class="offer-money"><label>Preço (R$)<input name="price" type="number" min="0" max="100000000" step="0.01" required placeholder="0,00"></label><label>Frete (R$)<input name="shipping" type="number" min="0" max="100000000" step="0.01" required value="0"></label><label>Desconto (R$)<input name="discount" type="number" min="0" max="100000000" step="0.01" required value="0"></label></div><label>Forma de pagamento<select name="payment"><option>Pix</option><option>Cartão de crédito</option><option>Boleto</option><option>Débito</option></select></label><label>Condição da oferta<select name="condition"><option>Novo</option><option>Usado</option><option>Recondicionado</option></select></label></div><label class="checkbox-label"><input type="checkbox" name="imported"> Produto importado (preço com tributos)</label><div class="offer-total">Total: <output>R$ 0,00</output></div>`;
  $('#offer-rows').append(section);
  if (state.preferences?.payments.length) $('[name=payment]', section).value = state.preferences.payments[0];
}
async function openResearch(id) {
  state.preferences = await api('/api/preferences');
  const p = productById(id), form = $('#research-form');
  form.reset();
  form.elements.product_id.value = p.id;
  form.elements.verified_at.value = localNow();
  form.elements.verified_at.max = localNow();
  $('#research-product').textContent = `${p.name}${state.preferences.cep ? ` · Frete para o CEP ${state.preferences.cep}` : ' · Informe o frete para o seu CEP'}`;
  $('.form-error', form).textContent = '';
  $('#offer-rows').replaceChildren();
  addOffer();
  $('#research-dialog').showModal();
}
async function openPreferences() {
  const data = await api('/api/preferences');
  state.preferences = data;
  const provider = await api('/api/search-provider');
  $('#search-provider-status').textContent = provider.configured ? 'Busca ampliada ativada.' : 'Busca ampliada ainda não configurada.';
  $('#preferences-form').elements.search_api_key.value = '';
  const form = $('#preferences-form');
  form.elements.cep.value = data.cep;
  $$('[name=payments]', form).forEach(input => input.checked = data.payments.includes(input.value));
  form.elements.imported.checked = data.imported;
  form.elements.used.checked = data.used;
  $('.form-error', form).textContent = '';
  $('#preferences-dialog').showModal();
}
async function submit(form, action) {
  const button = $('button[type=submit]', form);
  button.disabled = true;
  $('.form-error', form).textContent = '';
  try { await action(); } catch (error) { $('.form-error', form).textContent = error.message; }
  finally { button.disabled = false; }
}
$('#product-form').addEventListener('submit', event => {
  event.preventDefault();
  const form = event.target;
  submit(form, async () => {
    const files = [...form.elements.photos.files];
    if (files.length > 6 || files.reduce((sum, f) => sum + f.size, 0) > 19 * 1024 * 1024) throw new Error('Envie até 6 fotos, somando no máximo 19 MB.');
    const data = Object.fromEntries(new FormData(form));
    delete data.photos;
    const id = form.elements.id.value;
    const saved = await api(`/api/products${id ? `/${id}` : ''}`, { method: id ? 'PUT' : 'POST', body: JSON.stringify(data) });
    form.elements.id.value = saved.id;
    if (files.length) {
      const upload = new FormData();
      files.forEach(file => upload.append('photos', file));
      try { await api(`/api/products/${saved.id}/photos`, { method: 'POST', body: upload }); }
      catch (error) { await load(); throw new Error(`O produto foi salvo. As fotos não foram enviadas: ${error.message} Você pode tentar salvar novamente.`); }
    }
    await load();
    $('#product-dialog').close();
    toast(productById(saved.id)?.archived ? 'Alterações salvas. O produto continua arquivado.' : saved.collection?.status === 'deferred' ? 'Produto salvo. A busca automática está programada para a janela do dia 15 em diante.' : 'Produto atualizado. Pesquisa solicitada conforme a agenda e a cota.');
  });
});
$('#research-form').addEventListener('submit', event => {
  event.preventDefault();
  const form = event.target;
  submit(form, async () => {
    const offers = $$('.offer-row', form).map(row => {
      const data = {};
      $$('input,select', row).forEach(input => data[input.name] = input.type === 'checkbox' ? input.checked : input.value);
      return data;
    });
    await api(`/api/products/${form.elements.product_id.value}/searches`, { method: 'POST', body: JSON.stringify({ verified_at: new Date(form.elements.verified_at.value).toISOString(), notes: form.elements.notes.value, offers }) });
    await load();
    $('#research-dialog').close();
    toast('Pesquisa registrada. Mais um ponto no seu histórico.');
    await openDetail(form.elements.product_id.value);
  });
});
$('#preferences-form').addEventListener('submit', event => {
  event.preventDefault();
  const form = event.target;
  submit(form, async () => {
    state.preferences = await api('/api/preferences', { method: 'PUT', body: JSON.stringify({ cep: form.elements.cep.value, payments: $$('[name=payments]:checked', form).map(i => i.value), imported: form.elements.imported.checked, used: form.elements.used.checked }) });
    $('#preferences-dialog').close();
    toast('Preferências atualizadas.');
  });
});
document.addEventListener('click', event => {
  const closer = event.target.closest('.close');
  if (closer) { closer.closest('dialog').close(); return; }
  const target = event.target.closest('[data-action]');
  if (!target) return;
  run(async () => {
    const { action, id } = target.dataset;
    if (action === 'new') openProduct();
    if (action === 'detail') await openDetail(id);
    if (action === 'edit') { if ($('#detail-dialog').open) $('#detail-dialog').close(); openProduct(id); }
    if (action === 'collect') {
      target.disabled = true;
      try {
        await api(`/api/products/${id}/collect`, { method: 'POST' });
        await load();
        if ($('#detail-dialog').open) await openDetail(id);
        toast('Consulta solicitada. Os resultados aparecerão automaticamente.');
      } finally { target.disabled = false; }
    }
    if (action === 'research') { $('#detail-dialog').close(); await openResearch(id); }
    if (action === 'archive') {
      const p = productById(id);
      await api(`/api/products/${id}/archive`, { method: 'PATCH', body: JSON.stringify({ archived: !p.archived }) });
      await load();
      await openDetail(id);
      toast(p.archived ? 'Produto reativado.' : 'Produto arquivado. O histórico continua salvo.');
    }
    if (action === 'remove-offer') {
      if ($$('.offer-row').length === 1) { toast('A pesquisa precisa de pelo menos uma oferta.'); return; }
      target.closest('.offer-row').remove();
      $$('.offer-number').forEach((span, index) => span.textContent = index + 1);
    }
  });
});
document.addEventListener('keydown', event => {
  if ((event.key === 'Enter' || event.key === ' ') && event.target.matches('.product-image')) { event.preventDefault(); run(() => openDetail(event.target.dataset.id)); }
});
$('#offer-rows').addEventListener('input', event => {
  const row = event.target.closest('.offer-row');
  const value = name => Math.round(Number($(`[name=${name}]`, row).value || 0) * 100);
  $('output', row).textContent = money(value('price') + value('shipping') - value('discount'));
});
$('#add-offer').addEventListener('click', () => { if ($$('.offer-row').length < 50) addOffer(); });
$('#new-product').addEventListener('click', () => openProduct());
$('#nav-products').addEventListener('click', () => { $('#main').scrollIntoView({ behavior: 'smooth' }); $('#filter').focus(); });
$('#nav-preferences').addEventListener('click', () => run(openPreferences));
$('#save-search-provider').addEventListener('click', () => run(async () => {
  const input = $('#preferences-form').elements.search_api_key;
  const button = $('#save-search-provider');
  button.disabled = true;
  $('#search-provider-status').textContent = 'Validando a conta gratuita…';
  try {
    const result = await api('/api/search-provider', {method:'PUT', body:JSON.stringify({api_key:input.value.trim()})});
    input.value = '';
    $('#search-provider-status').textContent = `Busca ampliada ativada. ${result.remaining} consultas disponíveis na cota Free.`;
    toast('Busca ampliada ativada. Pesquise o produto novamente.');
  } catch (error) {
    $('#search-provider-status').textContent = error.message;
  } finally { button.disabled = false; }
}));
$('#remove-search-provider').addEventListener('click', () => run(async () => {
  await api('/api/search-provider', {method:'DELETE'});
  $('#preferences-form').elements.search_api_key.value = '';
  $('#search-provider-status').textContent = 'Busca ampliada desativada. A chave foi removida do servidor.';
}));
$('#filter').addEventListener('input', render);
$('#status-filter').addEventListener('change', render);
load().catch(error => {
  $('#products').innerHTML = '<div class="empty-state"><h3>Não foi possível carregar seus produtos</h3><p>Confira se os contêineres estão funcionando e recarregue esta página.</p></div>';
  toast(error.message);
});

function updateProductQuery() {
  const form = $('#product-form');
  const colors = form.elements.preferred_colors.value.split(',').map(c => c.trim()).filter(Boolean);
  const model = form.elements.model.value.trim() || 'modelo';
  const brand = form.elements.brand.value.trim() || 'marca';
  const size = form.elements.desired_size.value.trim();
  $('#product-query-preview').textContent = `"${model}" ${brand}${size ? ` ${size}` : ''}${colors.length ? ` (${colors.map(c => `"${c}"`).join(' OR ')})` : ''} comprar`;
}
$('#product-form').addEventListener('input', updateProductQuery);

function coverageNotice(product) {
  const count = product.coverage?.confirmed_stores || 0;
  const shopping = (product.collection?.report || []).flatMap(r => r.shopping_results || []);
  const current = product.price_summary.current_cents;
  const lower = current != null && shopping.some(o => o.indexed_price_cents < current);
  const message = count === 1 ? 'Comparação limitada: preço confirmado em apenas 1 loja. Isso não define a melhor compra.' : count > 1 ? `Preços confirmados em ${count} lojas. A cobertura ainda pode ser parcial.` : 'Ainda não há preços confirmados para estes critérios.';
  return `<div class="coverage-notice"><strong>${esc(message)}</strong>${lower ? '<p>Há anúncios com preços inferiores a confirmar. Confira o Google Shopping nos detalhes.</p>' : ''}</div>`;
}
function shoppingResults(product) {
  const items = (product.collection?.report || []).flatMap(r => r.shopping_results || []);
  if (!items.length) return '';
  return `<section class="purchase-section shopping-section"><h3 class="details-subtitle">Anúncios encontrados no Google Shopping</h3><p class="helper">Valores anunciados pelo Google, sem confirmação para seu tamanho, cor, pagamento ou CEP. Não entram no histórico até serem verificados na loja.</p><div class="shopping-grid">${items.map(item => `<article class="shopping-result"><strong>${esc(item.store)}</strong><p>${esc(item.title)}</p><b>${money(item.indexed_price_cents)}</b><small>Preço anunciado · variação e condições a confirmar</small><a class="button" href="${esc(item.merchant_url || item.google_url)}" target="_blank" rel="noopener noreferrer">${item.merchant_url ? 'Conferir na loja ↗' : 'Ver anúncio no Google ↗'}</a></article>`).join('')}</div></section>`;
}

function renderMonitoring() {
  const monitor=state.monitoring;
  if (!monitor) return;
  $('#monitor-phase').textContent = monitor.phase === 'paused' ? 'Monitoramento pausado · dias 1 a 14' : monitor.phase === 'api' ? 'Janela de compra · API e fontes públicas' : 'Monitoramento em fontes públicas · sem SerpApi';
  const next=new Date(monitor.next_run).toLocaleString('pt-BR',{timeZone:'America/Sao_Paulo',dateStyle:'short',timeStyle:'short'});
  $('#monitor-next').textContent=`Próximo horário: ${next} · Brasília. Telegram: ${monitor.telegram.linked ? 'conectado' : 'não conectado'}.`;
  $('#monitor-budget').textContent=`Reserva conservadora do mês: ${monitor.reserved_units}/${monitor.total_budget} consultas. Limite para automáticas: ${monitor.automatic_budget}; restante reservado para uso manual.`;
}
function renderTelegram(data) {
  $('#telegram-status').textContent=data.linked ? `Conectado ao bot @${data.bot_username}. Os próximos alertas poderão ser enviados.` : data.pairing_url ? 'Abra seu bot, toque em Iniciar e depois confirme o vínculo.' : 'Telegram ainda não conectado.';
  const link=$('#telegram-open');
  link.hidden=!data.pairing_url;
  if(data.pairing_url) link.href=data.pairing_url;
  else link.removeAttribute('href');
  $('#telegram-test').disabled=!data.linked;
  $('#telegram-link').disabled=!data.pairing_url;
}
async function openAlerts() {
  const [telegram,monitor]=await Promise.all([api('/api/telegram'),api('/api/monitoring')]);
  $('#telegram-token').value='';
  renderTelegram(telegram);
  const labels={pending:'Aguardando envio',sending:'Enviando',sent:'Enviado',failed:'Falha no envio',not_sent:'Não enviado'};
  $('#alerts-list').innerHTML=monitor.alerts.length ? monitor.alerts.map(alert=>`<article class="alert-item"><strong>${esc(labels[alert.status]||alert.status)} · ${esc(dateTime(alert.created_at))}</strong><p>${esc(alert.body)}</p>${alert.error ? `<small>${esc(alert.error)}</small>` : ''}<a class="button" href="${esc(alert.url)}" target="_blank" rel="noopener noreferrer">Ver produto na loja ↗</a></article>`).join('') : '<p class="helper">Nenhum alerta gerado. O primeiro aviso depende de uma oferta com total e condições confirmados.</p>';
  $('#alerts-dialog').showModal();
}
async function telegramAction(button,action) {
  button.disabled=true;
  try { await action(); }
  catch(error) { $('#telegram-status').textContent=error.message; }
  finally { button.disabled=false; }
}
$('#nav-alerts').addEventListener('click',()=>run(openAlerts));
$('#configure-alerts').addEventListener('click',()=>run(openAlerts));
$('#telegram-prepare').addEventListener('click',event=>telegramAction(event.target,async()=>{
  const data=await api('/api/telegram',{method:'POST',body:JSON.stringify({token:$('#telegram-token').value.trim()})});
  $('#telegram-token').value='';renderTelegram(data);
}));
$('#telegram-link').addEventListener('click',event=>telegramAction(event.target,async()=>{
  renderTelegram(await api('/api/telegram/link',{method:'POST'})); await load();
}));
$('#telegram-test').addEventListener('click',event=>telegramAction(event.target,async()=>{
  await api('/api/telegram/test',{method:'POST'});$('#telegram-status').textContent='Mensagem de teste enviada. Confira a conversa no Telegram.';
}));
$('#telegram-remove').addEventListener('click',event=>telegramAction(event.target,async()=>{
  renderTelegram(await api('/api/telegram',{method:'DELETE'}));await load();
}));
