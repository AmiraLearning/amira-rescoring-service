/**
 * Comprehensive validation utilities for CDK constructs
 */

export class ValidationError extends Error {
  constructor(message: string, public readonly context?: any) {
    super(message);
    this.name = 'ValidationError';
  }
}

export interface ValidationResult {
  isValid: boolean;
  errors: string[];
  warnings: string[];
}

/**
 * Validation utilities for common CDK patterns
 */
export class CDKValidationUtils {

  /**
   * Validate S3 bucket name follows AWS naming conventions
   */
  static validateS3BucketName(bucketName: string): ValidationResult {
    const errors: string[] = [];
    const warnings: string[] = [];

    // Basic naming rules
    if (bucketName.length < 3 || bucketName.length > 63) {
      errors.push('S3 bucket name must be between 3 and 63 characters');
    }

    if (!/^[a-z0-9.-]+$/.test(bucketName)) {
      errors.push('S3 bucket name can only contain lowercase letters, numbers, dots, and hyphens');
    }

    if (bucketName.startsWith('.') || bucketName.endsWith('.')) {
      errors.push('S3 bucket name cannot start or end with a dot');
    }

    if (bucketName.includes('..')) {
      errors.push('S3 bucket name cannot contain two adjacent dots');
    }

    if (/^\d+\.\d+\.\d+\.\d+$/.test(bucketName)) {
      errors.push('S3 bucket name cannot be formatted as an IP address');
    }

    // Best practice warnings
    if (bucketName.includes('.')) {
      warnings.push('S3 bucket names with dots may cause SSL certificate validation issues');
    }

    return { isValid: errors.length === 0, errors, warnings };
  }

  /**
   * Validate Lambda function configuration
   */
  static validateLambdaConfig(config: {
    memorySize?: number;
    timeout?: number;
    concurrency?: number;
  }): ValidationResult {
    const errors: string[] = [];
    const warnings: string[] = [];

    // Memory validation
    if (config.memorySize) {
      if (config.memorySize < 128 || config.memorySize > 10240) {
        errors.push('Lambda memory must be between 128MB and 10,240MB');
      }

      // Check if memory is in 64MB increments above 3008MB
      if (config.memorySize > 3008 && config.memorySize % 64 !== 0) {
        errors.push('Lambda memory above 3008MB must be in 64MB increments');
      }

      // Warning for very high memory
      if (config.memorySize > 5000) {
        warnings.push(`High Lambda memory allocation (${config.memorySize}MB) may be cost-inefficient`);
      }
    }

    // Timeout validation
    if (config.timeout) {
      if (config.timeout < 1 || config.timeout > 900) {
        errors.push('Lambda timeout must be between 1 second and 15 minutes');
      }

      // Warning for very long timeouts
      if (config.timeout > 300) {
        warnings.push(`Long Lambda timeout (${config.timeout}s) may indicate need for async processing`);
      }
    }

    // Concurrency validation
    if (config.concurrency) {
      if (config.concurrency < 1 || config.concurrency > 1000) {
        errors.push('Lambda concurrency should be between 1 and 1000');
      }

      if (config.concurrency > 100) {
        warnings.push(`High Lambda concurrency (${config.concurrency}) may impact other functions`);
      }
    }

    return { isValid: errors.length === 0, errors, warnings };
  }

  /**
   * Validate VPC configuration
   */
  static validateVpcConfig(config: {
    natGatewayCount?: number;
    enableInterfaceEndpoints?: boolean;
  }): ValidationResult {
    const errors: string[] = [];
    const warnings: string[] = [];

    if (config.natGatewayCount !== undefined) {
      if (config.natGatewayCount < 0 || config.natGatewayCount > 3) {
        errors.push('NAT Gateway count should be between 0 and 3');
      }

      if (config.natGatewayCount === 0 && !config.enableInterfaceEndpoints) {
        warnings.push('No NAT Gateways and no VPC endpoints may prevent internet access');
      }

      if (config.natGatewayCount > 2) {
        warnings.push(`${config.natGatewayCount} NAT Gateways may be cost-inefficient for most workloads`);
      }
    }

    return { isValid: errors.length === 0, errors, warnings };
  }

  /**
   * Validate stage configuration
   */
  static validateStageConfig(stage: string): ValidationResult {
    const errors: string[] = [];
    const warnings: string[] = [];

    const validStages = ['dev', 'development', 'test', 'staging', 'stage', 'prod', 'production'];

    if (!validStages.includes(stage.toLowerCase())) {
      errors.push(`Invalid stage '${stage}'. Valid stages: ${validStages.join(', ')}`);
    }

    if (stage.toLowerCase() === 'prod' || stage.toLowerCase() === 'production') {
      warnings.push('Production stage detected - ensure all security and compliance requirements are met');
    }

    return { isValid: errors.length === 0, errors, warnings };
  }

  /**
   * Validate lifecycle rules for cost optimization
   */
  static validateS3LifecycleRules(rules: any[]): ValidationResult {
    const errors: string[] = [];
    const warnings: string[] = [];

    if (!rules || rules.length === 0) {
      warnings.push('No lifecycle rules defined - consider adding rules for cost optimization');
      return { isValid: true, errors, warnings };
    }

    for (const rule of rules) {
      if (!rule.id) {
        errors.push('Lifecycle rule missing required ID');
        continue;
      }

      if (rule.transitions) {
        const transitions = Array.isArray(rule.transitions) ? rule.transitions : [rule.transitions];

        for (const transition of transitions) {
          if (!transition.storageClass) {
            errors.push(`Lifecycle rule '${rule.id}' has transition without storage class`);
          }

          if (!transition.transitionAfter) {
            errors.push(`Lifecycle rule '${rule.id}' has transition without transition period`);
          }

          // Check for cost optimization opportunities
          if (transition.storageClass === 'STANDARD_IA' &&
              transition.transitionAfter &&
              transition.transitionAfter < 30) {
            warnings.push(`Lifecycle rule '${rule.id}' transitions to IA before 30 days - objects must be stored for at least 30 days`);
          }
        }
      }
    }

    return { isValid: errors.length === 0, errors, warnings };
  }

  /**
   * Validate security group rules
   */
  static validateSecurityGroupRules(rules: {
    allowedPorts?: number[];
    allowedCidrs?: string[];
    description?: string;
  }): ValidationResult {
    const errors: string[] = [];
    const warnings: string[] = [];

    if (rules.allowedCidrs) {
      for (const cidr of rules.allowedCidrs) {
        if (cidr === '0.0.0.0/0') {
          warnings.push('Security group allows access from anywhere (0.0.0.0/0) - ensure this is intentional');
        }

        // Basic CIDR format validation
        if (!/^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\/\d{1,2}$/.test(cidr)) {
          errors.push(`Invalid CIDR format: ${cidr}`);
        }
      }
    }

    if (rules.allowedPorts) {
      for (const port of rules.allowedPorts) {
        if (port < 1 || port > 65535) {
          errors.push(`Invalid port number: ${port} (must be 1-65535)`);
        }

        // Warn about commonly abused ports
        const dangerousPorts = [22, 3389, 5432, 3306, 1433, 6379, 9200];
        if (dangerousPorts.includes(port)) {
          warnings.push(`Port ${port} is commonly targeted - ensure proper access controls`);
        }
      }
    }

    if (!rules.description || rules.description.length < 10) {
      warnings.push('Security group rule should have a descriptive description');
    }

    return { isValid: errors.length === 0, errors, warnings };
  }

  /**
   * Combined validation for construct props
   */
  static validateConstructProps(props: any, constructName: string): ValidationResult {
    const errors: string[] = [];
    const warnings: string[] = [];

    if (!props) {
      errors.push(`${constructName} props cannot be null or undefined`);
      return { isValid: false, errors, warnings };
    }

    // Check for required properties based on construct type
    if (constructName.includes('Lambda') && !props.timeout && !props.memorySize) {
      warnings.push(`${constructName} using default timeout and memory - consider explicit configuration`);
    }

    if (constructName.includes('Storage') && !props.lifecycleRules) {
      warnings.push(`${constructName} without lifecycle rules may incur unnecessary costs`);
    }

    if (constructName.includes('Networking') && props.natGatewayCount === undefined) {
      warnings.push(`${constructName} NAT Gateway count not specified - using default`);
    }

    return { isValid: errors.length === 0, errors, warnings };
  }
}

/**
 * Validation aspect that applies comprehensive validation to all constructs
 */
export class ComprehensiveValidationAspect {
  private readonly stage: string;

  constructor(stage: string) {
    this.stage = stage;
  }

  visit(node: any): void {
    // Skip validation for certain node types
    if (!node.node || typeof node.node.addMetadata !== 'function') {
      return;
    }

    const validationResults: ValidationResult[] = [];

    // Apply stage validation
    const stageValidation = CDKValidationUtils.validateStageConfig(this.stage);
    if (!stageValidation.isValid) {
      validationResults.push(stageValidation);
    }

    // Add validation results as metadata
    if (validationResults.length > 0) {
      node.node.addMetadata('validation-results', {
        timestamp: new Date().toISOString(),
        stage: this.stage,
        results: validationResults,
        appliedBy: 'ComprehensiveValidationAspect'
      });

      // Log warnings and errors
      validationResults.forEach(result => {
        result.warnings.forEach(warning => console.warn(`[${node.node.id}] ${warning}`));
        result.errors.forEach(error => console.error(`[${node.node.id}] ${error}`));
      });
    }
  }
}
