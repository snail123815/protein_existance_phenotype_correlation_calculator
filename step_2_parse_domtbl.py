# Filter base on:
# 1. E-values per protein hit (full protein E)
# 2. Length difference (percentage of diff / min(tlen, qlen))
# 3. Domain coverage
# 3.1. Domains with high E-values (dom E) will not be included
# Coverage is calculated base on all filtered domain hits. The method has many
# limitations. E.g. If one protein has scattered domain hits, these will be
# combined, overlap removed, summed to calculate the coverage. I will not
# distingush its direction, orientation, function correlation etc. But since
# hmmer search of single protein does not consider domain founction, it could
# be a good way of taking ** gapped domain hits ** into consideration.

import re
from collections import OrderedDict, namedtuple
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from subprocess import run
from threading import Thread

import pandas as pd
from tqdm import tqdm

from project_settings import (DOMTBLOUT_P, GATHER_DOMTBL_P, GATHER_MATCH_P,
                              GATHER_T_COV, GATHER_T_DOME, GATHER_T_E,
                              LEN_DIFF, NCPU)

# DOMTBLOUT_P = Path("domtblout_nodup.txt")
# DOMTBLOUT_P = Path("domtblout_partial.txt")
# DOMTBLOUT_P = Path("domtblout_partial_nodup.txt")


# Output pathes


def cal_cov(line: dict, dom_cov_regions: list[int]):
    """
    Run within each single protein lines
    Only the alignment of domains with E values lower than threshold
    will be included in the coverage calculation
    Each coverage will be converted to range of positions (int)
    Combine all positions -> deduplicate -> count = lenth of covered region

    """
    dom_covq = dom_covt = 0
    is_end_dom = False
    if line["dom_total"] == 1:  # Single domain
        is_end_dom = True
        if line["dom_i_E"] <= GATHER_T_DOME:
            cov_len = line["ali_to"] - line["ali_from"] + 1
            dom_covq = cov_len / line["qlen"]
            dom_covt = cov_len / line["tlen"]
    else:  # multi domains
        # if line['dom_n'] == 1:  # first multi domain
        #     dom_cov_regions = []
        if line["dom_i_E"] <= GATHER_T_DOME:
            dom_cov_regions.extend(
                list(range(line["ali_from"], line["ali_to"] + 1))
            )
        if line["dom_n"] == line["dom_total"]:  # end of all domains
            is_end_dom = True
            cov_len = len(set(dom_cov_regions))
            dom_covq = cov_len / line["qlen"]
            dom_covt = cov_len / line["tlen"]
    return is_end_dom, dom_cov_regions, dom_covq, dom_covt


def remove_duplicates(file_p: Path) -> Path:
    no_dup_path = file_p.parent / f"{file_p.stem}_nodup{file_p.suffix}"
    print(f"Removing duplicates from file {file_p} -> {no_dup_path}")
    ddup = run(f"awk '!seen[$0]++' {file_p} > {no_dup_path}", shell=True)
    ddup.check_returncode()
    return no_dup_path


def read_domtbl(domtbl_p: Path) -> pd.DataFrame:
    def parse_one_line(
        l,
        domtbl_dict,
        t_e=GATHER_T_E,
        len_diff=LEN_DIFF,
        splitter=re.compile(r" +"),
    ):
        line_list = splitter.split(l.strip())

        full_E = float(line_list[6])
        if full_E > t_e:
            return
        else:
            tlen = int(line_list[2])
            qlen = int(line_list[5])
            if abs(tlen - qlen) / min(tlen, qlen) > len_diff:
                return
            try:
                domtbl_dict["tp"].append(line_list[0])
                domtbl_dict["qp"].append(line_list[3])
                domtbl_dict["tlen"].append(tlen)
                domtbl_dict["qlen"].append(qlen)
                domtbl_dict["ali_from"].append(int(line_list[17]))
                domtbl_dict["ali_to"].append(int(line_list[18]))
                domtbl_dict["dom_i_E"].append(float(line_list[12]))
                domtbl_dict["dom_n"].append(int(line_list[9]))
                domtbl_dict["dom_total"].append(int(line_list[10]))
                domtbl_dict["full_E"].append(full_E)
                domtbl_dict["anno"].append(" ".join(line_list[22:]))
            except ValueError as ve:
                print(l)
                raise ve

    print(f"Counting lines in domtblout from jackhmmer: {domtbl_p}")
    domtbl_len = int(
        run(["wc", "-l", domtbl_p], capture_output=True)
        .stdout.decode()
        .split(" ")[0]
    )
    print(f"{domtbl_len} lines in total.")
    domtbl_dict = {}
    header = [
        "tp",
        "qp",
        "tlen",
        "qlen",
        "ali_from",
        "ali_to",
        "dom_i_E",
        "dom_n",
        "dom_total",
        "full_E",
        "anno",
    ]
    for h in header:
        domtbl_dict[h] = []
    with domtbl_p.open("rt") as dt:
        for l in tqdm(dt, desc="Reading file", total=domtbl_len):
            if l.startswith("#"):
                continue
            parse_one_line(l, domtbl_dict)
    print("Converting data to dataframe...", end="")
    domtbl_df = pd.DataFrame(domtbl_dict)
    print("Done.")
    return domtbl_df


def process_single_query(domtbl_q: list[OrderedDict]):
    qp = domtbl_q[0]["qp"]
    dom_cov_regions = []
    match_dict = {
        "Query": [],
        "Target strain": [],
        "Target protein": [],
        "Coverage on Query": [],
        "Coverage on Target": [],
        "Expect protein": [],
        "Target description": [],
    }
    for row in domtbl_q:
        is_end_dom, dom_cov_regions, dom_covq, dom_covt = cal_cov(
            row, dom_cov_regions
        )
        if is_end_dom:
            if dom_covq < GATHER_T_COV or dom_covt < GATHER_T_COV:
                continue
            dom_cov_regions = []
        else:
            continue

        target_strain = row["tp"].split("_")[0]
        target_protein = row["tp"][len(target_strain) + 1 :]
        match_dict["Query"].append(qp)
        match_dict["Target strain"].append(target_strain)
        match_dict["Target protein"].append(target_protein)
        match_dict["Coverage on Query"].append(dom_covq)
        match_dict["Coverage on Target"].append(dom_covt)
        match_dict["Expect protein"].append(row["full_E"])
        match_dict["Target description"].append(
            row["anno"].replace(target_protein, "").strip()
        )
    return pd.DataFrame(match_dict)


def parse_dom_table_mt(domtbl_df):
    previous_qp = ""
    domtbl_q: list[namedtuple] = []
    with ProcessPoolExecutor(NCPU) as executer:
        futures = []
        for domrow in tqdm(
            domtbl_df.itertuples(),
            desc="Submitting jobs",
            total=domtbl_df.shape[0],
        ):
            dom_dict = domrow._asdict()
            if domrow.qp == previous_qp:
                domtbl_q.append(dom_dict)
            else:
                # submit previous one
                if len(domtbl_q) > 0:
                    futures.append(
                        executer.submit(process_single_query, domtbl_q)
                    )
                # empty q
                domtbl_q = [dom_dict]
                previous_qp = domrow.qp
        # submit the last one
        if len(domtbl_q) > 0:
            futures.append(executer.submit(process_single_query, domtbl_q))
        results = []
        for future in tqdm(futures, desc="Processing per protein hits"):
            results.append(future.result())
    return pd.concat(results, ignore_index=True)


if __name__ == "__main__":
    if "nodup" in str(DOMTBLOUT_P):
        nodup_domtbl = DOMTBLOUT_P
    else:
        nodup_domtbl = remove_duplicates(DOMTBLOUT_P)
    domtbl_df = read_domtbl(nodup_domtbl)
    print(f"Write re-formated domain hit table {GATHER_DOMTBL_P}")

    def write_reformated_domtbl():
        domtbl_df.to_csv(GATHER_DOMTBL_P, sep="\t")

    write_reformated_domtbl_thread = Thread(target=write_reformated_domtbl)
    write_reformated_domtbl_thread.start()
    match_df = parse_dom_table_mt(domtbl_df)
    assert (
        match_df.duplicated(
            subset=["Query", "Target strain", "Target protein"]
        ).sum()
        == 0
    )
    write_reformated_domtbl_thread.join()
    print(f"Write match table {GATHER_MATCH_P}")
    match_df.to_csv(GATHER_MATCH_P, sep="\t")
