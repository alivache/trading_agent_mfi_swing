# Ipoteze pre-înregistrate — 14 noiembrie 2026

Scrise **înainte** de rularea oricărui test din acest director. Regulile și criteriile de mai jos
nu se modifică după ce văd rezultatele. Orice variantă testată ulterior se raportează separat,
ca explorare, nu ca rezultat principal.

## Cadru comun

- **Date:** bare SIP Alpaca (planul gratuit permite istoricul SIP mai vechi de 15 minute), din
  4 ianuarie 2016 până la 13 noiembrie 2026.
- **Perioade:** *calibrare* 2016–2021 (doar verificare de bun-simț, parametrii vin din literatură,
  nu se optimizează), *verificare* 2022 – 13 nov 2026. **Decide doar perioada de verificare.**
- **Execuție fără privit în viitor:** un semnal calculat la închiderea zilei *t* se execută la
  deschiderea zilei *t+1* (H1, H2) sau la închiderea zilei *t* doar dacă semnalul folosește
  exclusiv date până la *t* inclusiv și ordinul e de tip „market on close” (H3).
- **Costuri:** ETF-uri 0.05% pe sens (0.10% dus-întors); acțiuni cu gap 0.25% pe sens (0.50%
  dus-întors), fără taxa de împrumut pentru short (necunoscută; limitare declarată).
- **Bani neinvestiți:** randament 0 (conservator; în realitate ar câștiga dobânda).
- **Repere:** cumpărare și păstrare SPY, respectiv QQQ, pe aceeași perioadă.

## H1 — Urmărirea trendului pe indice

- **Regula:** dețin SPY când închiderea e peste media simplă pe 200 de zile, altfel stau în cash.
  La fel, separat, pentru QQQ.
- **Ce măsor:** randament anual (CAGR), volatilitate, Sharpe (fără rata fără risc), cădere maximă,
  procentul timpului investit, număr de tranzacții.
- **Trece dacă**, în perioada de verificare, pentru SPY **și** pentru QQQ:
  Sharpe ≥ Sharpe-ul cumpărării și păstrării **și** căderea maximă ≤ 70% din cea a cumpărării
  și păstrării.

## H2 — Rotație pe sectoare după momentum

- **Univers:** cele 9 ETF-uri sectoriale SPDR existente din 2016: XLB, XLE, XLF, XLI, XLK, XLP,
  XLU, XLV, XLY.
- **Regula:** la ultima zi de bursă a fiecărei luni, clasez sectoarele după randamentul pe
  6 luni excluzând ultima lună (de la t−126 la t−21 zile de bursă); dețin primele 3, ponderi
  egale, din deschiderea următoarei zile. Filtru absolut: un sector din top 3 cu randament
  negativ pe 6–1 luni e înlocuit cu cash.
- **Trece dacă**, în perioada de verificare: randament anual peste SPY **și** Sharpe peste SPY.

## H3 — Revenire pe termen scurt la indice

- **Regula (Connors, RSI pe 2 zile):** cumpăr SPY la închidere când RSI(2) < 10 și închiderea e
  peste media pe 200 de zile; vând la închidere când închiderea trece peste media pe 5 zile.
  La fel, separat, pentru QQQ. O singură poziție odată.
- **Ce măsor:** câștigul mediu pe tranzacție după costuri, rata de câștig, timpul investit,
  Sharpe calculat pe toate zilele (inclusiv cele în cash).
- **Trece dacă**, în perioada de verificare, pentru SPY **și** QQQ: câștigul mediu pe tranzacție
  după costuri > 0 cu t > 2 **și** Sharpe-ul strategiei ≥ Sharpe-ul cumpărării și păstrării.

## H4 — Short pe gap-urile în sus (întoarcerea gap-ului)

Motivat de replay-ul aug–nov 2026 (gap-urile în sus au pierdut −2.47% pe candidat față de QQQ,
t = −4.6). Aici se verifică dacă efectul există pe mulți ani și dacă rezistă unui stop realist.

- **Univers:** toate acțiunile americane obișnuite de pe NYSE/NASDAQ/AMEX, **inclusiv cele delistate
  între timp** (dacă Alpaca le are în lista de active inactive), ca să reduc partinirea
  supraviețuitorilor.
- **Candidați într-o zi:** gap la deschidere = deschidere / închiderea anterioară − 1 ≥ 5%;
  închiderea anterioară ≥ 5 USD; media valorii tranzacționate pe ultimele 20 de zile ≥ 5 milioane USD
  (lichiditate și șanse mai bune de împrumut). Maxim 10 pe zi, după valoarea tranzacționată.
  Gap-ul la deschidere e cunoscut la 9:30, momentul intrării, deci nu e privit în viitor.
- **Regula:** short la prețul de deschidere al primei bare de 1 minut; **stop la +10%** față de
  intrare, verificat minut cu minut (dacă o bară deschide deja peste stop, execuția e la
  deschiderea ei, nu la stop); altfel acoperire la închiderea barei de 15:49 (închidere − 10 min;
  12:49 în zilele scurte). Cost 0.50% dus-întors.
- **Ce măsor:** câștigul mediu pe tranzacție, mediana, rata de câștig, media pe zile și t-ul ei,
  procentul de zile pozitive, rezultatul pe fiecare an, cea mai rea zi. Stopurile de 5% și 20% se
  raportează doar ca sensibilitate, nu decid.
- **Trece dacă**, în perioada de verificare: media zilnică > 0 după costuri cu t > 2 **și**
  cel puțin 4 din cei 5 ani (2022–2026) pozitivi.
- **Limitări declarate:** nu știu ce acțiuni erau ușor de împrumutat istoric; taxa de împrumut nu e
  inclusă; opririle de tranzacționare apar doar ca lipsă de bare.

## Ce urmează după rezultate

- Ce nu trece criteriul **nu** se construiește, oricât de aproape ar fi.
- Ce trece se construiește întâi ca robot pe paper, cu aceleași reguli, și se compară live cu
  testul înainte de orice altceva.
