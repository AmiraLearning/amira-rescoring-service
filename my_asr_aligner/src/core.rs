use std::collections::{HashMap, HashSet, VecDeque};
use similar::{ChangeTag, TextDiff};
use rustc_hash::FxHashMap;

pub const DASH: &str = "-";

pub type ErrorCache = FxHashMap<(String, String), bool>;

lazy_static::lazy_static! {
    static ref COMBINED_ACCEPTED_PHONEMES: HashMap<&'static str, HashSet<&'static str>> = {
        let valid_pronunciation_map: HashMap<&'static str, Vec<&'static str>> = [
            ("a_letter", vec!["a"]),
            ("a_sound", vec!["æ"]),
            ("b_letter", vec!["bi"]),
            ("b_sound", vec!["bʌ", "b"]),
            ("c_letter", vec!["si"]),
            ("c_sound", vec!["k", "s", "kʌ", "sʌ"]),
            ("d_letter", vec!["di"]),
            ("d_sound", vec!["dʌ", "d"]),
            ("e_letter", vec!["i"]),
            ("e_sound", vec!["ɛ", "i"]),
            ("f_letter", vec!["ɛf"]),
            ("f_sound", vec!["fʌ", "f"]),
            ("g_letter", vec!["ji"]),
            ("g_sound", vec!["gʌ", "jʌ", "g", "j"]),
            ("h_letter", vec!["ax"]),
            ("h_sound", vec!["hʌ", "h"]),
            ("i_letter", vec!["γ"]),
            ("i_sound", vec!["ɪ", "γ"]),
            ("j_letter", vec!["ja"]),
            ("j_sound", vec!["jʌ", "j"]),
            ("k_letter", vec!["ka"]),
            ("k_sound", vec!["kʌ", "k"]),
            ("l_letter", vec!["ɛl"]),
            ("l_sound", vec!["l", "ʌl"]),
            ("m_letter", vec!["ɛm"]),
            ("m_sound", vec!["m", "mʌ"]),
            ("n_letter", vec!["ɛn"]),
            ("n_sound", vec!["n", "nʌ"]),
            ("o_letter", vec!["o"]),
            ("o_sound", vec!["ɑ", "o"]),
            ("p_letter", vec!["pi"]),
            ("p_sound", vec!["pʌ", "ph", "p"]),
            ("q_letter", vec!["kyu"]),
            ("q_sound", vec!["kwʌ", "kʌ", "kw"]),
            ("r_letter", vec!["ɑɹ"]),
            ("r_sound", vec!["ɝ", "ɹʌ", "ɹ"]),
            ("s_letter", vec!["ɛs"]),
            ("s_sound", vec!["s", "sʌ"]),
            ("t_letter", vec!["ti"]),
            ("t_sound", vec!["tʌ", "th", "t"]),
            ("u_letter", vec!["yu"]),
            ("u_sound", vec!["ʌ", "u"]),
            ("v_letter", vec!["vi"]),
            ("v_sound", vec!["v", "vʌ"]),
            ("w_letter", vec!["dʌbʌlyu"]),
            ("w_sound", vec!["wʌ", "w"]),
            ("x_letter", vec!["ɛks"]),
            ("x_sound", vec!["ks"]),
            ("y_letter", vec!["wγ"]),
            ("y_sound", vec!["yʌ", "y"]),
            ("z_letter", vec!["zi"]),
            ("z_sound", vec!["z", "zʌ"]),
        ].iter().cloned().collect();

        let acceptance_map: HashMap<&'static str, Vec<&'static str>> = [
            ("a_letter", vec!["ɛ"]),
            ("a_sound", vec!["ɛ", "ʌ"]),
            ("b_letter", vec!["b"]),
            ("b_sound", vec![]),
            ("c_letter", vec!["zi"]),
            ("c_sound", vec![]),
            ("d_letter", vec![]),
            ("d_sound", vec![]),
            ("e_letter", vec![]),
            ("e_sound", vec!["ɪ", "æ"]),
            ("f_letter", vec!["ɛs"]),
            ("f_sound", vec!["v", "vʌ"]),
            ("g_letter", vec![]),
            ("g_sound", vec![]),
            ("h_letter", vec!["x"]),
            ("h_sound", vec![]),
            ("i_letter", vec!["ɑ"]),
            ("i_sound", vec!["ɛ", "i"]),
            ("j_letter", vec![]),
            ("j_sound", vec![]),
            ("k_letter", vec![]),
            ("k_sound", vec![]),
            ("l_letter", vec![]),
            ("l_sound", vec!["ʌ"]),
            ("m_letter", vec!["ɛn"]),
            ("m_sound", vec![]),
            ("n_letter", vec!["ɪn", "ɛm"]),
            ("n_sound", vec!["m"]),
            ("o_letter", vec![]),
            ("o_sound", vec!["ʌ", "γ", "ɑɹ"]),
            ("p_letter", vec![]),
            ("p_sound", vec![]),
            ("q_letter", vec![]),
            ("q_sound", vec![]),
            ("r_letter", vec![]),
            ("r_sound", vec!["ʌ", "l", "ʌl"]),
            ("s_letter", vec![]),
            ("s_sound", vec!["f"]),
            ("t_letter", vec![]),
            ("t_sound", vec!["pʌ"]),
            ("u_letter", vec!["y"]),
            ("u_sound", vec!["ɑ", "ʌl", "ɝ"]),
            ("v_letter", vec![]),
            ("v_sound", vec!["z"]),
            ("w_letter", vec!["dʌbyu"]),
            ("w_sound", vec![]),
            ("x_letter", vec![]),
            ("x_sound", vec![]),
            ("y_letter", vec![]),
            ("y_sound", vec![]),
            ("z_letter", vec!["z"]),
            ("z_sound", vec!["v"]),
        ].iter().cloned().collect();

        let mut map = HashMap::new();
        for (key, valid_phonemes) in valid_pronunciation_map {
            let mut all_phonemes: HashSet<&'static str> = valid_phonemes.into_iter().collect();
            if let Some(accepted_phonemes) = acceptance_map.get(key) {
                all_phonemes.extend(accepted_phonemes.iter().cloned());
            }
            map.insert(key, all_phonemes);
        }
        map
    };
}

pub fn is_error_cached(item_name: &str, hyp_phoneme: &str, cache: &mut ErrorCache) -> bool {
    let key = (item_name.to_string(), hyp_phoneme.to_string());

    if let Some(&result) = cache.get(&key) {
        return result;
    }

    let result = if let Some(accepted_phonemes_set) = COMBINED_ACCEPTED_PHONEMES.get(item_name) {
        !accepted_phonemes_set.contains(hyp_phoneme)
    } else {
        true
    };

    cache.insert(key, result);
    result
}

pub fn is_error(item_name: &str, hyp_phoneme: &str) -> bool {
    if let Some(accepted_phonemes_set) = COMBINED_ACCEPTED_PHONEMES.get(item_name) {
        !accepted_phonemes_set.contains(hyp_phoneme)
    } else {
        true
    }
}

pub fn push_result(result: &mut Vec<String>, errors: &mut Vec<bool>, confidence: &mut Vec<f32>,
               phoneme: &str, is_error: bool, conf: f32) {
    result.push(phoneme.to_string());
    errors.push(is_error);
    confidence.push(conf);
}

pub fn push_dash_result(result: &mut Vec<String>, errors: &mut Vec<bool>, confidence: &mut Vec<f32>) {
    result.push(DASH.to_string());
    errors.push(true);
    confidence.push(0.0);
}

/// Core alignment function without PyO3 dependencies
pub fn word_level_alignment_core(
    expected_items: Vec<String>,
    ref_phons: Vec<String>,
    hyp_phons: Vec<String>,
    confidences: Vec<f32>,
) -> Result<(Vec<String>, Vec<bool>, Vec<f32>), String> {
    // Early exit for identical sequences
    if ref_phons == hyp_phons {
        return Ok((hyp_phons.clone(), vec![false; hyp_phons.len()], confidences));
    }

    let ref_refs: Vec<&str> = ref_phons.iter().map(|s| s.as_str()).collect();
    let hyp_refs: Vec<&str> = hyp_phons.iter().map(|s| s.as_str()).collect();
    let diff = TextDiff::configure()
        .algorithm(similar::Algorithm::Myers)
        .diff_slices(&ref_refs, &hyp_refs);

    // Pre-allocate with estimated capacity
    let estimated_capacity = ref_phons.len() + hyp_phons.len();
    let mut word_alignment_result: Vec<String> = Vec::with_capacity(estimated_capacity);
    let mut errors: Vec<bool> = Vec::with_capacity(estimated_capacity);
    let mut matched_confidence: Vec<f32> = Vec::with_capacity(estimated_capacity);

    let mut ri: usize = 0;
    let mut hj: usize = 0;
    let mut pending_ref_indices: VecDeque<usize> = VecDeque::new();
    let mut error_cache: ErrorCache = FxHashMap::default();

    for op in diff.ops() {
        for change in diff.iter_changes(op) {
            match change.tag() {
                ChangeTag::Equal => {
                    // Process all pending deletions as dashes
                    while let Some(_idx) = pending_ref_indices.pop_front() {
                        push_dash_result(&mut word_alignment_result, &mut errors, &mut matched_confidence);
                    }

                    if hj < hyp_phons.len() {
                        let conf = if hj < confidences.len() { confidences[hj] } else { 0.0 };
                        push_result(&mut word_alignment_result, &mut errors, &mut matched_confidence,
                                   &hyp_phons[hj], false, conf);
                    }
                    ri += 1;
                    hj += 1;
                }
                ChangeTag::Delete => {
                    pending_ref_indices.push_back(ri);
                    ri += 1;
                }
                ChangeTag::Insert => {
                    let conf = if hj < confidences.len() { confidences[hj] } else { 0.0 };
                    if let Some(ref_idx) = pending_ref_indices.pop_front() {
                        let ref_word = if ref_idx < expected_items.len() {
                            &expected_items[ref_idx]
                        } else if ref_idx < ref_phons.len() {
                            &ref_phons[ref_idx]
                        } else {
                            ""
                        };
                        let hyp_word = if hj < hyp_phons.len() { &hyp_phons[hj] } else { "" };

                        let is_err = is_error_cached(ref_word, hyp_word, &mut error_cache);

                        if is_err {
                            push_dash_result(&mut word_alignment_result, &mut errors, &mut matched_confidence);
                        } else {
                            push_result(&mut word_alignment_result, &mut errors, &mut matched_confidence,
                                       hyp_word, false, conf);
                        }
                    } else {
                        let hyp_word = if hj < hyp_phons.len() { &hyp_phons[hj] } else { "" };
                        let ref_word_ctx = if ri < expected_items.len() {
                            &expected_items[ri]
                        } else if ri < ref_phons.len() {
                            &ref_phons[ri]
                        } else {
                            ""
                        };
                        let is_err = is_error_cached(ref_word_ctx, hyp_word, &mut error_cache);
                        push_result(
                            &mut word_alignment_result,
                            &mut errors,
                            &mut matched_confidence,
                            hyp_word,
                            is_err,
                            conf,
                        );
                    }
                    hj += 1;
                }
            }
        }
    }

    // Process remaining pending deletions
    while let Some(_idx) = pending_ref_indices.pop_front() {
        push_dash_result(&mut word_alignment_result, &mut errors, &mut matched_confidence);
    }

    Ok((word_alignment_result, errors, matched_confidence))
}

/// Confidence-aware alignment wrapper (optional behavior controlled by flag)
pub fn word_level_alignment_core_with_confidence(
    expected_items: Vec<String>,
    ref_phons: Vec<String>,
    hyp_phons: Vec<String>,
    confidences: Vec<f32>,
    enable_confidence_weighting: bool,
) -> Result<(Vec<String>, Vec<bool>, Vec<f32>), String> {
    if !enable_confidence_weighting {
        return word_level_alignment_core(expected_items, ref_phons, hyp_phons, confidences);
    }

    // For now, reuse existing core for structure but bias confidences during INSERT handling:
    // higher-confidence hyp tokens will avoid dash; lower-confidence more likely to dash.
    // This is a minimal DRY approach; deeper weighting can be added internally later.

    match word_level_alignment_core(expected_items, ref_phons, hyp_phons, confidences) {
        Ok((tokens, errors, mut confs)) => {
            for i in 0..confs.len() {
                if errors[i] {
                    confs[i] *= 0.5;
                }
            }
            Ok((tokens, errors, confs))
        }
        Err(e) => Err(e),
    }
}
