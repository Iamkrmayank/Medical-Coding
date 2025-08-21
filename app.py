# app.py — Gooclaim Coding Agent → FHIR Bundle (MVP with CPT + HCPCS + performed-procedure rules)
# Run:  streamlit run app.py
# Reqs: pip install streamlit

import os, json, uuid, re
from datetime import datetime
import streamlit as st

st.set_page_config(page_title="Gooclaim Coding → FHIR", page_icon="🧾", layout="centered")
st.title("🧾 Gooclaim — Medical Coding Agent (FHIR Bundle MVP)")
st.caption("Intake → ICD‑10‑CM + CPT/HCPCS with evidence-aware 'performed' detection → FHIR Bundle (Claim + Condition [+ Procedure]).")

# ---------------------------
# Dictionaries (very small demo lexicon)
# ---------------------------
CPT_MAP = {
    # Imaging
    "chest x-ray single view": {"code":"71045", "display":"Radiologic examination, chest; single view", "system":"http://www.ama-assn.org/go/cpt"},
    "chest x-ray two views":  {"code":"71046", "display":"Radiologic examination, chest; 2 views",       "system":"http://www.ama-assn.org/go/cpt"},

    # Office/EM defaults (we populate separately but leave here for reference)
    "em_99213": {"code":"99213","display":"Office/outpatient visit, established patient, low MDM","system":"http://www.ama-assn.org/go/cpt"},

    # Respiratory therapy
    "nebulizer treatment": {"code":"94640","display":"Pressurized or nonpressurized inhalation treatment for acute airway obstruction","system":"http://www.ama-assn.org/go/cpt"},

    # Injections (generic placeholder; in real systems you'd pick precise admin codes)
    "therapeutic injection": {"code":"96372","display":"Therapeutic, prophylactic, or diagnostic injection; subcutaneous or intramuscular","system":"http://www.ama-assn.org/go/cpt"},
}

HCPCS_MAP = {
    # Common drug/supply examples (illustrative only)
    "albuterol neb": {"code":"J7613","display":"Albuterol, inhalation solution, FDA-approved final product, non-compounded, 1 mg","system":"https://www.cms.gov/mcd/hcpcs"},
    "ceftriaxone inj": {"code":"J0696","display":"Injection, ceftriaxone sodium, per 250 mg","system":"https://www.cms.gov/mcd/hcpcs"},
    "dexamethasone inj": {"code":"J1100","display":"Injection, dexamethasone sodium phosphate, 1 mg","system":"https://www.cms.gov/mcd/hcpcs"},
    # Vaccines & admins often have G-codes or specific CPTs; this is just a demo subset
}

PERFORMED_HINTS = [
    "performed", "done", "completed", "administered", "given", "provided", "carried out",
    "obtained", "obtained in clinic", "obtained today"
]

# ---------------------------
# Rule engine (demo)
# ---------------------------
def present(text: str, *terms) -> bool:
    low = text.lower()
    return all(t in low for t in terms)

def near(text: str, a: str, b: str, window: int = 30) -> bool:
    """Return True if term b appears within +/-window chars of term a (rough heuristic)."""
    low = text.lower()
    a_idx = low.find(a.lower())
    if a_idx == -1:
        return False
    start = max(0, a_idx - window)
    end   = min(len(low), a_idx + len(a) + window)
    return b.lower() in low[start:end]

def detect_performed_cxr(note: str):
    """Detect CHEST X-RAY performed; decide CPT (single vs two views) based on language."""
    low = note.lower()
    # only if presence of performed hints near 'x-ray'
    if "x-ray" in low or "xray" in low or "cxr" in low:
        if any(near(low, "x-ray", h, 40) or near(low, "xray", h, 40) or near(low, "cxr", h, 40) for h in PERFORMED_HINTS):
            # crude rules for views
            if "two views" in low or "2 views" in low or "pa and lateral" in low:
                return CPT_MAP["chest x-ray two views"]
            return CPT_MAP["chest x-ray single view"]
    return None

def detect_nebulizer_and_drug(note: str):
    """Detect a nebulizer treatment performed + the albuterol supply (HCPCS)."""
    low = note.lower()
    performed = any(h in low for h in PERFORMED_HINTS)
    if ("nebulizer" in low or "neb" in low) and performed:
        cpt = CPT_MAP["nebulizer treatment"]
        # detect albuterol content
        if "albuterol" in low:
            hcpcs = HCPCS_MAP["albuterol neb"]
        else:
            hcpcs = None
        return cpt, hcpcs
    return None, None

def detect_injection_and_drug(note: str):
    """Detect IM/SC injection performed + example HCPCS for common drugs."""
    low = note.lower()
    if ("intramuscular" in low or "im injection" in low or "injection given" in low or "shot given" in low) and any(h in low for h in PERFORMED_HINTS):
        cpt = CPT_MAP["therapeutic injection"]
        # crude drug matches
        if "ceftriaxone" in low:
            return cpt, HCPCS_MAP["ceftriaxone inj"]
        if "dexamethasone" in low:
            return cpt, HCPCS_MAP["dexamethasone inj"]
        return cpt, None
    return None, None

def tiny_rules(envelope: dict):
    """
    Returns:
      icd10 (list[dict]),
      cpt_em (dict),
      procedures (list[dict of CPT]),
      hcpcs (list[dict])
    """
    note = (envelope.get("clinical_note") or {}).get("text_preview", "")
    low  = note.lower()

    icd10 = []
    procedures = []
    hcpcs = []

    # Primary Dx: acute bronchitis pattern
    if present(low, "cough") and (present(low, "wheez") or present(low, "shortness of breath")) and (present(low, "fever") or present(low, "azithromycin")):
        icd10.append({
            "code": "J20.9",
            "display": "Acute bronchitis, unspecified organism",
            "system": "http://hl7.org/fhir/sid/icd-10-cm",
            "rank": 1,
            "confidence": 0.72,
            "rationale": "Acute cough + fever/wheeze/SOB; outpatient; antibiotic prescribed."
        })

    # Secondary symptoms
    if "wheez" in low:
        icd10.append({"code":"R06.2","display":"Wheezing","system":"http://hl7.org/fhir/sid/icd-10-cm","rank":2,"confidence":0.62,"rationale":"Documented wheezing."})
    if "shortness of breath" in low:
        icd10.append({"code":"R06.02","display":"Shortness of breath","system":"http://hl7.org/fhir/sid/icd-10-cm","rank":3,"confidence":0.60,"rationale":"Documented SOB."})
    if "fever" in low:
        icd10.append({"code":"R50.9","display":"Fever, unspecified","system":"http://hl7.org/fhir/sid/icd-10-cm","rank":4,"confidence":0.58,"rationale":"Documented fever."})
    if "cough" in low:
        icd10.append({"code":"R05.9","display":"Cough, unspecified","system":"http://hl7.org/fhir/sid/icd-10-cm","rank":5,"confidence":0.56,"rationale":"Documented cough."})

    # CPT E/M (MVP default)
    cpt_em = {
        "code": "99213",
        "display": "Office or other outpatient visit for an established patient, low MDM",
        "system": "http://www.ama-assn.org/go/cpt",
        "assumed_patient_status": "established",
        "confidence": 0.58,
        "rationale": "Acute uncomplicated illness, outpatient management."
    }

    # --- Performed procedure detection ---
    # 1) Chest X-ray actually performed?
    cxr = detect_performed_cxr(note)
    if cxr:
        procedures.append(cxr)

    # 2) Nebulizer treatment performed (+ possible albuterol supply)
    neb_cpt, albut_hcpcs = detect_nebulizer_and_drug(note)
    if neb_cpt:
        procedures.append(neb_cpt)
    if albut_hcpcs:
        hcpcs.append(albut_hcpcs)

    # 3) Therapeutic injection performed (+ possible ceftriaxone/dexamethasone supply)
    inj_cpt, drug_hcpcs = detect_injection_and_drug(note)
    if inj_cpt:
        procedures.append(inj_cpt)
    if drug_hcpcs:
        hcpcs.append(drug_hcpcs)

    # Guardrail: if only "recommend" test without performed hints, do not add
    # (The detector above already requires PERFORMED_HINTS near target words.)
    # For your sample intake note, the CXR is not added because it’s only recommended:
    # "Recommend chest X-ray and prescribe azithromycin."  (no 'performed' hint)
    # (Ref: claim_envelope clinical_note.text_preview)  <-- See sample JSON
    return icd10, cpt_em, procedures, hcpcs

# ---------------------------
# FHIR Builders
# ---------------------------
def fhir_ref(resource_type: str, rid: str) -> dict:
    return {"reference": f"{resource_type}/{rid}"}

def build_fhir_bundle(envelope: dict, icd10: list, cpt_em: dict, procedures: list, hcpcs: list) -> dict:
    """
    Bundle (type: collection):
      - Patient
      - Encounter
      - Claim (diagnosis[], procedure[], item[] for CPT + HCPCS, supportingInfo)
      - Condition (for each ICD-10)
    """
    pat_id = f"pat-{uuid.uuid4().hex[:8]}"
    enc_id = f"enc-{uuid.uuid4().hex[:8]}"
    clm_id = f"clm-{uuid.uuid4().hex[:8]}"

    patient = envelope.get("patient", {})
    encounter = envelope.get("encounter", {})
    claim_id_input = envelope.get("claim_id") or clm_id

    fhir_patient = {
        "resourceType": "Patient",
        "id": pat_id,
        "identifier": [{"system":"urn:mrn","value": patient.get("mrn","")}],
        "name": [{"family": patient.get("last_name",""), "given": [patient.get("first_name","")]}],
        "gender": {"M":"male","F":"female"}.get(patient.get("sex","").upper(), "unknown"),
        "birthDate": patient.get("dob","")
    }

    fhir_encounter = {
        "resourceType": "Encounter",
        "id": enc_id,
        "status": "finished",
        "class": {"system":"http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB", "display": "ambulatory"},
        "subject": fhir_ref("Patient", pat_id),
        "period": {"start": encounter.get("date"), "end": encounter.get("date")},
        "serviceProvider": {"identifier": {"system":"urn:npi:location", "value": encounter.get("location_npi","")}}
    }

    # Conditions + Claim.diagnosis[]
    condition_entries = []
    claim_diagnosis = []
    for idx, d in enumerate(icd10, start=1):
        cond_id = f"cond-{uuid.uuid4().hex[:8]}"
        condition = {
            "resourceType": "Condition",
            "id": cond_id,
            "subject": fhir_ref("Patient", pat_id),
            "encounter": fhir_ref("Encounter", enc_id),
            "code": {
                "coding": [{
                    "system": d.get("system","http://hl7.org/fhir/sid/icd-10-cm"),
                    "code": d["code"],
                    "display": d.get("display","")
                }],
                "text": d.get("display","")
            },
            "verificationStatus": {"coding":[{"system":"http://terminology.hl7.org/CodeSystem/condition-ver-status","code":"confirmed"}]},
            "clinicalStatus": {"coding":[{"system":"http://terminology.hl7.org/CodeSystem/condition-clinical","code":"active"}]},
            "note": [{"text": f"rank={d.get('rank')}, confidence={d.get('confidence')}, rationale={d.get('rationale','')}"}]
        }
        condition_entries.append({"resource": condition})
        claim_diagnosis.append({
            "sequence": idx,
            "diagnosisReference": fhir_ref("Condition", cond_id),
            "type": [{"coding":[{"system":"http://terminology.hl7.org/CodeSystem/ex-diagnosistype","code":"principal" if idx==1 else "additional"}]}]
        })

    claim = {
        "resourceType": "Claim",
        "id": clm_id,
        "status": "active",
        "type": {"coding":[{"system":"http://terminology.hl7.org/CodeSystem/claim-type","code":"professional"}]},
        "use": "claim",
        "patient": fhir_ref("Patient", pat_id),
        "created": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "enterer": {"identifier": {"system":"urn:npi:provider","value": encounter.get("provider_npi","")}},
        "insurer": {"display": patient.get("payer_name","")},
        "priority": {"coding":[{"system":"http://terminology.hl7.org/CodeSystem/processpriority","code":"normal"}]},
        "diagnosis": claim_diagnosis,
        "procedure": [],
        "item": [],
        "supportingInfo": [],
        "extension": [
            {
                "url": "urn:gooclaim:meta",
                "extension": [
                    {"url":"claim_id_input","valueString": str(claim_id_input)},
                    {"url":"source","valueString": str(envelope.get("meta",{}).get("source",""))}
                ]
            }
        ]
    }

    # Add E/M item
    if cpt_em and cpt_em.get("code"):
        claim["item"].append({
            "sequence": len(claim["item"]) + 1,
            "productOrService": {
                "coding": [{
                    "system": cpt_em.get("system", "http://www.ama-assn.org/go/cpt"),
                    "code": cpt_em["code"],
                    "display": cpt_em.get("display","")
                }],
                "text": cpt_em.get("display","")
            },
            "servicedDate": encounter.get("date"),
            "encounter": [fhir_ref("Encounter", enc_id)]
        })
        claim["supportingInfo"].append({
            "sequence": len(claim["supportingInfo"]) + 1,
            "category": {"coding":[{"system":"http://terminology.hl7.org/CodeSystem/claiminformationcategory","code":"info"}]},
            "valueString": f"E/M assumes patient status={cpt_em.get('assumed_patient_status','unknown')} (confidence={cpt_em.get('confidence')})"
        })

    # Add performed procedures (CPT) into both Claim.procedure[] and Claim.item[]
    for i, p in enumerate(procedures, start=1):
        claim["procedure"].append({
            "sequence": i,
            "date": encounter.get("date"),
            "procedureCodeableConcept": {
                "coding": [{
                    "system": p.get("system","http://www.ama-assn.org/go/cpt"),
                    "code": p["code"],
                    "display": p.get("display","")
                }]
            }
        })
        claim["item"].append({
            "sequence": len(claim["item"]) + 1,
            "productOrService": {
                "coding": [{
                    "system": p.get("system","http://www.ama-assn.org/go/cpt"),
                    "code": p["code"],
                    "display": p.get("display","")
                }],
                "text": p.get("display","")
            },
            "servicedDate": encounter.get("date"),
            "encounter": [fhir_ref("Encounter", enc_id)]
        })

    # Add HCPCS supplies/drugs as separate items
    for h in hcpcs:
        claim["item"].append({
            "sequence": len(claim["item"]) + 1,
            "productOrService": {
                "coding": [{
                    "system": h.get("system","https://www.cms.gov/mcd/hcpcs"),
                    "code": h["code"],
                    "display": h.get("display","")
                }],
                "text": h.get("display","")
            },
            "servicedDate": encounter.get("date"),
            "encounter": [fhir_ref("Encounter", enc_id)]
        })

    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "id": f"bundle-{uuid.uuid4().hex[:8]}",
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entry": [
            {"resource": fhir_patient},
            {"resource": fhir_encounter},
            {"resource": claim},
            *condition_entries
        ]
    }
    return bundle

# ---------------------------
# UI
# ---------------------------
st.subheader("1) Load Intake Output (claim_envelope.json)")
default_sample_path = "/mnt/data/claim_envelope.json"  # Use your local/sample path
uploaded = st.file_uploader("Upload Intake JSON", type=["json"])
use_sample = st.checkbox("Use sample at /mnt/data/claim_envelope.json", value=True)

envelope = None
if uploaded:
    try:
        envelope = json.load(uploaded)
    except Exception as e:
        st.error(f"Invalid JSON: {e}")
elif use_sample and os.path.exists(default_sample_path):
    with open(default_sample_path, "r") as f:
        envelope = json.load(f)

if envelope:
    st.success("Intake envelope loaded.")
    with st.expander("Preview Intake JSON"):
        st.json(envelope)

    st.subheader("2) Generate FHIR Bundle (with CPT + HCPCS when performed)")
    if st.button("Run Coding Agent → FHIR"):
        icd10, cpt_em, procedures, hcpcs = tiny_rules(envelope)
        bundle = build_fhir_bundle(envelope, icd10, cpt_em, procedures, hcpcs)

        st.success("FHIR Bundle ready.")
        with st.expander("View FHIR JSON"):
            st.json(bundle)

        st.download_button(
            "⬇️ Download FHIR Bundle (JSON)",
            data=json.dumps(bundle, indent=2).encode("utf-8"),
            file_name="gooclaim_coding_fhir_bundle.json",
            mime="application/json"
        )

        # Quick summary
        st.markdown("### Summary")
        if icd10:
            st.write("**Diagnoses (ICD‑10‑CM):**")
            for d in icd10:
                st.write(f"- **{d['code']}** — {d.get('display','')}")
        else:
            st.write("**Diagnoses:** _none_")

        st.write(f"**E/M (CPT):** {cpt_em.get('code','—')} — {cpt_em.get('display','')}")
        if procedures:
            st.write("**Performed Procedures (CPT):**")
            for p in procedures:
                st.write(f"- **{p['code']}** — {p.get('display','')}")
        else:
            st.write("**Performed Procedures:** _none detected_")

        if hcpcs:
            st.write("**HCPCS (Supplies/Drugs):**")
            for h in hcpcs:
                st.write(f"- **{h['code']}** — {h.get('display','')}")
        else:
            st.write("**HCPCS:** _none detected_")
else:
    st.info("Upload an Intake JSON or tick the sample checkbox to proceed.")
