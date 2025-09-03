use pyo3::prelude::*;
use rayon::prelude::*;

pub mod core;
use core::{word_level_alignment_core, word_level_alignment_core_with_confidence};

#[derive(Debug, Clone, PartialEq)]
#[allow(dead_code)] // Used in tests and future optimizations
pub enum PhonemeMatch {
    Exact(String),
    Accepted(String),
    Error,
}

/// Aligns ASR transcriptions with the reference text using Myers diff (via `similar`).
/// Each step is interpreted as Equal, Replace, Delete, or Insert.
/// Optimized version with pre-allocation, VecDeque, and caching.
#[pyfunction(signature = (expected_items, ref_phons, hyp_phons, confidences, enable_confidence_weighting=false))]
#[pyo3(text_signature = "(expected_items, ref_phons, hyp_phons, confidences, enable_confidence_weighting=False)")]
fn word_level_alignment(
    expected_items: Vec<String>,
    ref_phons: Vec<String>,
    hyp_phons: Vec<String>,
    confidences: Vec<f32>,
    enable_confidence_weighting: bool,
) -> PyResult<(Vec<String>, Vec<bool>, Vec<f32>)> {
    match word_level_alignment_core_with_confidence(
        expected_items,
        ref_phons,
        hyp_phons,
        confidences,
        enable_confidence_weighting,
    ) {
        Ok(result) => Ok(result),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

/// Batch processing function for multiple alignments with parallel processing
#[pyfunction(signature = (batch_expected_items, batch_ref_phons, batch_hyp_phons, batch_confidences, enable_confidence_weighting=false))]
#[pyo3(text_signature = "(batch_expected_items, batch_ref_phons, batch_hyp_phons, batch_confidences, enable_confidence_weighting=False)")]
fn batch_word_level_alignment(
    batch_expected_items: Vec<Vec<String>>,
    batch_ref_phons: Vec<Vec<String>>,
    batch_hyp_phons: Vec<Vec<String>>,
    batch_confidences: Vec<Vec<f32>>,
    enable_confidence_weighting: bool,
) -> PyResult<Vec<(Vec<String>, Vec<bool>, Vec<f32>)>> {
    // Validate input lengths
    if batch_expected_items.len() != batch_ref_phons.len() ||
       batch_ref_phons.len() != batch_hyp_phons.len() ||
       batch_hyp_phons.len() != batch_confidences.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "All batch input vectors must have the same length"
        ));
    }

    // Process in parallel using rayon
    let results: Result<Vec<_>, _> = (0..batch_expected_items.len())
        .into_par_iter()
        .map(|i| -> PyResult<(Vec<String>, Vec<bool>, Vec<f32>)> {
            match word_level_alignment_core_with_confidence(
                batch_expected_items[i].clone(),
                batch_ref_phons[i].clone(),
                batch_hyp_phons[i].clone(),
                batch_confidences[i].clone(),
                enable_confidence_weighting,
            ) {
                Ok(result) => Ok(result),
                Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
            }
        })
        .collect();

    results
}

mod tests;

#[pymodule]
fn my_asr_aligner(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(word_level_alignment, m)?)?;
    m.add_function(wrap_pyfunction!(batch_word_level_alignment, m)?)?;
    Ok(())
}
