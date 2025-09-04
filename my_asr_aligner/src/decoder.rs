use pyo3::prelude::*;
use rayon::prelude::*;
use rustc_hash::FxHashMap;

#[derive(Debug, Clone)]
pub struct PhoneticTrie {
    root: TrieNode,
}

#[derive(Debug, Clone)]
struct TrieNode {
    // Using FxHashMap for faster hashing
    children: FxHashMap<String, TrieNode>,
    value: Option<String>,
}

impl TrieNode {
    fn new() -> Self {
        TrieNode {
            children: FxHashMap::default(),
            value: None,
        }
    }
}

impl PhoneticTrie {
    pub fn new(phonetic_elements: Vec<String>) -> Self {
        let mut root = TrieNode::new();

        for element in phonetic_elements {
            // Build trie from individual characters
            let tokens: Vec<String> = element.chars().map(|c| c.to_string()).collect();
            let mut current = &mut root;

            for token in &tokens {
                current = current
                    .children
                    .entry(token.clone())
                    .or_insert(TrieNode::new());
            }
            current.value = Some(element);
        }

        PhoneticTrie { root }
    }

    pub fn find_longest_match<'a>(
        &'a self,
        tokens: &[String],
        start_index: usize,
    ) -> (Option<&'a str>, usize) {
        let mut current = &self.root;
        let mut longest_match: Option<&'a str> = None;
        let mut tokens_consumed = 0;

        for (i, token) in tokens.iter().skip(start_index).enumerate() {
            if let Some(next_node) = current.children.get(token) {
                current = next_node;
                if let Some(ref value) = current.value {
                    longest_match = Some(value.as_str());
                    tokens_consumed = i + 1;
                }
            } else {
                break;
            }
        }

        (longest_match, tokens_consumed)
    }
}

#[pyclass]
pub struct RustPhonemeDecoder {
    trie: PhoneticTrie,
    separator_token: String,
    pad_token: String,
}

#[pymethods]
impl RustPhonemeDecoder {
    #[new]
    pub fn new(phonetic_elements: Vec<String>) -> Self {
        RustPhonemeDecoder {
            trie: PhoneticTrie::new(phonetic_elements),
            separator_token: "|".to_string(),
            pad_token: "<pad>".to_string(),
        }
    }

    pub fn decode(
        &self,
        pred_tokens: Vec<String>,
        max_probs: Option<Vec<f32>>,
    ) -> PyResult<(Vec<String>, Vec<f32>)> {
        let mut final_elements = Vec::with_capacity(pred_tokens.len() / 2); // Pre-allocate with estimate
        let mut final_confidences = Vec::with_capacity(pred_tokens.len() / 2);
        let mut current_segment_tokens = Vec::with_capacity(128); // Pre-allocate for typical segment size
        let mut current_segment_confidences = Vec::with_capacity(128);

        for (index, token) in pred_tokens.iter().enumerate() {
            if token == &self.pad_token {
                continue;
            } else if token == &self.separator_token {
                if !current_segment_tokens.is_empty() {
                    let (elements, confidences) = self
                        .process_segment(&current_segment_tokens, &current_segment_confidences)?;
                    final_elements.extend(elements);
                    final_confidences.extend(confidences);
                    current_segment_tokens.clear();
                    current_segment_confidences.clear();
                }
            } else {
                current_segment_tokens.push(token.clone());
                if let Some(ref probs) = max_probs {
                    if index < probs.len() {
                        current_segment_confidences.push(probs[index]);
                    }
                }
            }
        }

        // Process remaining segment
        if !current_segment_tokens.is_empty() {
            let (elements, confidences) =
                self.process_segment(&current_segment_tokens, &current_segment_confidences)?;
            final_elements.extend(elements);
            final_confidences.extend(confidences);
        }

        // Shrink to fit to reclaim excess memory
        final_elements.shrink_to_fit();
        final_confidences.shrink_to_fit();

        Ok((final_elements, final_confidences))
    }

    pub fn decode_batch(
        &self,
        batch_tokens: Vec<Vec<String>>,
        batch_probs: Option<Vec<Vec<f32>>>,
    ) -> PyResult<Vec<(Vec<String>, Vec<f32>)>> {
        // Use parallel processing for batch decoding
        if let Some(probs) = batch_probs {
            batch_tokens
                .into_par_iter()
                .zip(probs.into_par_iter())
                .map(|(tokens, p)| self.decode(tokens, Some(p)))
                .collect()
        } else {
            batch_tokens
                .into_par_iter()
                .map(|tokens| self.decode(tokens, None))
                .collect()
        }
    }
}

impl RustPhonemeDecoder {
    fn process_segment(
        &self,
        tokens: &[String],
        confidences: &[f32],
    ) -> PyResult<(Vec<String>, Vec<f32>)> {
        // Group consecutive tokens
        let (grouped_tokens, grouped_confidences) = self.group_consecutive(tokens, confidences);

        // Parse phonetic elements
        self.parse_phonetic_elements(&grouped_tokens, &grouped_confidences)
    }

    fn group_consecutive(&self, tokens: &[String], confidences: &[f32]) -> (Vec<String>, Vec<f32>) {
        if tokens.is_empty() {
            return (Vec::new(), Vec::new());
        }

        // Pre-allocate with worst-case size
        let mut grouped_tokens = Vec::with_capacity(tokens.len());
        let mut averaged_confidences = Vec::with_capacity(tokens.len());
        let has_confidences = !confidences.is_empty();

        let mut current_token = &tokens[0];
        let mut current_confidences: Vec<f32> = Vec::with_capacity(16); // Pre-allocate for typical run length

        if has_confidences {
            current_confidences.push(confidences[0]);
        }

        for i in 1..tokens.len() {
            let next_token = &tokens[i];
            if current_token == next_token {
                if has_confidences && i < confidences.len() {
                    current_confidences.push(confidences[i]);
                }
            } else {
                grouped_tokens.push(current_token.clone());
                if has_confidences && !current_confidences.is_empty() {
                    let avg =
                        current_confidences.iter().sum::<f32>() / current_confidences.len() as f32;
                    averaged_confidences.push(avg);
                }
                current_token = next_token;
                current_confidences.clear();
                if has_confidences && i < confidences.len() {
                    current_confidences.push(confidences[i]);
                }
            }
        }

        // Handle the last group
        grouped_tokens.push(current_token.clone());
        if has_confidences && !current_confidences.is_empty() {
            let avg = current_confidences.iter().sum::<f32>() / current_confidences.len() as f32;
            averaged_confidences.push(avg);
        }

        // Shrink to fit
        grouped_tokens.shrink_to_fit();
        averaged_confidences.shrink_to_fit();

        (grouped_tokens, averaged_confidences)
    }

    fn parse_phonetic_elements(
        &self,
        tokens: &[String],
        confidences: &[f32],
    ) -> PyResult<(Vec<String>, Vec<f32>)> {
        let mut final_elements = Vec::with_capacity(tokens.len());
        let mut final_confidences = Vec::with_capacity(tokens.len());
        let mut index = 0;
        let track_confidence = !confidences.is_empty();

        // Check for robust mode environment variable
        let robust_mode = std::env::var("DECODER_ROBUST_MODE")
            .map(|v| matches!(v.to_lowercase().as_str(), "1" | "true" | "yes" | "on"))
            .unwrap_or(false);

        while index < tokens.len() {
            let (matched_element, tokens_consumed) = self.trie.find_longest_match(tokens, index);

            if let Some(element) = matched_element {
                if tokens_consumed > 0 {
                    // Store owned string since we return it to Python
                    final_elements.push(element.to_string());
                    if track_confidence {
                        let segment_conf: &[f32] = &confidences[index..index + tokens_consumed];
                        let avg_conf = segment_conf.iter().sum::<f32>() / segment_conf.len() as f32;
                        final_confidences.push(avg_conf);
                    }
                    index += tokens_consumed;
                    continue;
                }
            }

            // Handle unmatched tokens - match Python decoder behavior
            let remaining_tokens: String = tokens[index..].join("");
            let message = format!(
                "Unable to match phonetic element at position {} in segment: {}",
                index, remaining_tokens
            );

            if robust_mode {
                // In robust mode, just skip unmatched tokens (like before)
                eprintln!("Warning: {}", message);
                index += 1;
                continue;
            } else {
                // In strict mode, raise error to match Python decoder behavior
                return Err(pyo3::exceptions::PyValueError::new_err(message));
            }
        }

        // Shrink to fit
        final_elements.shrink_to_fit();
        final_confidences.shrink_to_fit();

        Ok((final_elements, final_confidences))
    }
}
